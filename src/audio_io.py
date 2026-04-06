"""
音频输入输出 — 录音、设备检测、双工检测、重采样
解决 Windows 蓝牙 HFP 问题：
  - 使用 DirectSound 接口（兼容性最好）
  - 输入输出都走 HFP 通道（避免 A2DP/HFP 冲突）
  - 后台静音保活维持 HFP SCO 链路
"""

import sounddevice as sd
import numpy as np
import threading
import queue
import time
from typing import Optional, Callable

try:
    import soxr
    def fast_resample(audio: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
        """使用 soxr 高性能重采样（比 scipy FFT 快 5-10 倍）"""
        if from_sr == to_sr:
            return audio
        return soxr.resample(audio, from_sr, to_sr, quality='HQ').astype(np.float32)
    print("[Audio] 使用 soxr 高性能重采样")
except ImportError:
    from scipy.signal import resample as scipy_resample
    def fast_resample(audio: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
        """回退到 scipy 重采样"""
        if from_sr == to_sr:
            return audio
        target_len = int(len(audio) * to_sr / from_sr)
        return scipy_resample(audio, target_len).astype(np.float32)
    print("[Audio] soxr 未安装，回退到 scipy 重采样")


def list_devices():
    """列出所有音频设备"""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d['max_input_channels'] > 0 or d['max_output_channels'] > 0:
            hostapi = sd.query_hostapis(d['hostapi'])['name']
            io = f"in={d['max_input_channels']} out={d['max_output_channels']}"
            print(f"  #{i}: [{hostapi}] {d['name']}  {io}  sr={d['default_samplerate']}")


def find_bluetooth_devices(keyword: str = "漫步者") -> tuple[Optional[int], Optional[int], dict]:
    """(旧接口) 按关键词查找蓝牙设备"""
    devices = sd.query_devices()
    input_id = None
    output_id = None
    info = {}

    # 按优先级搜索: DirectSound > MME > WASAPI
    priority_order = ['DirectSound', 'MME', 'WASAPI']

    for target_api in priority_order:
        for i, d in enumerate(devices):
            name = d['name']
            if keyword not in name:
                continue
            hostapi = sd.query_hostapis(d['hostapi'])['name']
            if target_api not in hostapi:
                continue
            # 只选 Hands-Free (HFP) 设备，不选 Stereo (A2DP)
            if 'Stereo' in name:
                continue

            if d['max_input_channels'] > 0 and input_id is None:
                input_id = i
                info['input_name'] = name
                info['input_sr'] = int(d['default_samplerate'])
                info['input_api'] = hostapi

            if d['max_output_channels'] > 0 and output_id is None:
                output_id = i
                info['output_name'] = name
                info['output_sr'] = int(d['default_samplerate'])
                info['output_api'] = hostapi

        if input_id is not None and output_id is not None:
            break

    return input_id, output_id, info


def auto_detect_devices(prefer_local_input: bool = False, prefer_local: bool = False,
                        hfp_duplex: bool = False) -> dict:
    """自动检测最佳音频输入/输出设备

    策略（按优先级）：
      0. HFP全双工：输入输出都走 HFP（音质低但支持同时收发）
      1. 蓝牙一体设备：同一蓝牙设备的 HFP 输入 + A2DP Stereo 输出
      2. 蓝牙分体：任意蓝牙 HFP 输入 + 任意蓝牙 A2DP 输出
      3. 蓝牙+本地混合：蓝牙 HFP 输入 + 本地扬声器输出（或反之）
      4. 纯本地：系统默认输入 + 默认输出

    API 优先级：DirectSound > MME > WASAPI

    返回 dict:
      input_id, input_sr, input_name, input_api,
      output_id, output_sr, output_name, output_api,
      bt_name (蓝牙设备名, 无蓝牙时为 None),
      mode ('hfp_duplex' | 'bt_unified' | 'bt_split' | 'bt_mixed' | 'local')
    """
    devices = sd.query_devices()
    api_priority = {'Windows DirectSound': 0, 'DirectSound': 0,
                    'MME': 1, 'Windows WASAPI': 2, 'WASAPI': 2}

    # ---- 扫描所有设备，分类 ----
    bt_hfp_inputs = []   # (idx, dev, api_name, bt_name, priority)
    bt_hfp_outputs = []  # (idx, dev, api_name, bt_name, priority)
    bt_stereo_outputs = []  # (idx, dev, api_name, bt_name, priority)
    local_inputs = []    # (idx, dev, api_name, priority)
    local_outputs = []   # (idx, dev, api_name, priority)

    for i, d in enumerate(devices):
        name = d['name']
        hostapi = sd.query_hostapis(d['hostapi'])['name']
        pri = api_priority.get(hostapi, 9)

        # 跳过 WDM-KS（底层驱动接口，不稳定）
        if 'WDM' in hostapi:
            continue

        is_bt_hfp = 'Hands-Free' in name or 'hands-free' in name.lower()
        is_bt_stereo = 'Stereo' in name or 'stereo' in name.lower()
        is_bt = is_bt_hfp or is_bt_stereo

        # 提取蓝牙设备名（去掉 Hands-Free/Stereo 后缀和前缀装饰）
        bt_name = None
        if is_bt:
            import re as _re
            # 提取括号内的名称
            m = _re.search(r'[（(](.+?)[）)]', name)
            raw = m.group(1) if m else name
            # 去掉 Hands-Free/Stereo/AG Audio 等后缀
            raw = _re.sub(r'\s*(Hands-Free|Stereo|AG Audio|HF Audio).*', '', raw, flags=_re.IGNORECASE)
            bt_name = raw.strip()

        if is_bt_hfp and d['max_input_channels'] > 0:
            bt_hfp_inputs.append((i, d, hostapi, bt_name, pri))
        if is_bt_hfp and d['max_output_channels'] > 0:
            bt_hfp_outputs.append((i, d, hostapi, bt_name, pri))
        if is_bt_stereo and d['max_output_channels'] > 0:
            bt_stereo_outputs.append((i, d, hostapi, bt_name, pri))
        elif not is_bt:
            if d['max_input_channels'] > 0:
                # 排除 Mapper/主声音 等虚拟设备和立体声混音
                if 'Mapper' not in name and '主声音' not in name and '混音' not in name:
                    # 输入优先级：线路输入/麦克风 > 其他
                    in_pri = 0 if ('线路' in name or '麦克风' in name or
                                   'Line' in name or 'Mic' in name) else 1
                    local_inputs.append((i, d, hostapi, pri, in_pri))
            if d['max_output_channels'] > 0:
                if 'Mapper' not in name and '主声音' not in name:
                    # 输出优先级：扬声器/Speaker > HDMI/Digital
                    out_pri = 0 if ('扬声器' in name or 'Speaker' in name) else 1
                    local_outputs.append((i, d, hostapi, pri, out_pri))

    # 按 API 优先级排序
    bt_hfp_inputs.sort(key=lambda x: x[4])
    bt_hfp_outputs.sort(key=lambda x: x[4])
    bt_stereo_outputs.sort(key=lambda x: x[4])
    local_inputs.sort(key=lambda x: (x[4], x[3]))   # 先按设备类型优先级，再按API优先级
    local_outputs.sort(key=lambda x: (x[4], x[3]))

    # prefer_local: 跳过蓝牙，直接用本地设备
    if prefer_local:
        bt_hfp_inputs.clear()
        bt_hfp_outputs.clear()
        bt_stereo_outputs.clear()

    result = {}

    def _set_input(idx, dev, api):
        result['input_id'] = idx
        result['input_sr'] = int(dev['default_samplerate'])
        result['input_name'] = dev['name']
        result['input_api'] = api

    def _set_output(idx, dev, api):
        result['output_id'] = idx
        result['output_sr'] = int(dev['default_samplerate'])
        result['output_name'] = dev['name']
        result['output_api'] = api

    # ---- 策略0: HFP全双工（输入输出都走HFP，音质低但真全双工）----
    if hfp_duplex and bt_hfp_inputs and bt_hfp_outputs:
        # 优先找同名设备
        for hi, hd, hapi, hbt, _ in bt_hfp_inputs:
            for oi, od, oapi, obt, _ in bt_hfp_outputs:
                if hbt and obt and hbt == obt:
                    _set_input(hi, hd, hapi)
                    _set_output(oi, od, oapi)
                    result['bt_name'] = hbt
                    result['mode'] = 'hfp_duplex'
                    print(f"[Audio] HFP全双工模式: {hbt} (输入输出都走HFP，音质=电话级)")
                    print(f"  输入: #{hi} [{hapi}] {hd['name']} ({result['input_sr']}Hz)")
                    print(f"  输出: #{oi} [{oapi}] {od['name']} ({result['output_sr']}Hz)")
                    return result
        # 没有同名的，取第一个
        hi, hd, hapi, hbt, _ = bt_hfp_inputs[0]
        oi, od, oapi, obt, _ = bt_hfp_outputs[0]
        _set_input(hi, hd, hapi)
        _set_output(oi, od, oapi)
        result['bt_name'] = hbt or obt
        result['mode'] = 'hfp_duplex'
        print(f"[Audio] HFP全双工模式: {result['bt_name']} (输入输出都走HFP，音质=电话级)")
        print(f"  输入: #{hi} [{hapi}] {hd['name']} ({result['input_sr']}Hz)")
        print(f"  输出: #{oi} [{oapi}] {od['name']} ({result['output_sr']}Hz)")
        return result

    # ---- 分离模式：本地麦克风输入 + 蓝牙A2DP输出（同时工作）----
    if prefer_local_input and bt_stereo_outputs and local_inputs:
        li, ld, lapi, *_ = local_inputs[0]
        si, sd_dev, sapi, sbt, _ = bt_stereo_outputs[0]
        _set_input(li, ld, lapi)
        _set_output(si, sd_dev, sapi)
        result['bt_name'] = sbt
        result['mode'] = 'split'
        print(f"[Audio] 分离模式: 本地麦克风输入 + 蓝牙A2DP输出")
        print(f"  输入: #{li} [{lapi}] {ld['name']} ({result['input_sr']}Hz)")
        print(f"  输出: #{si} [{sapi}] {sd_dev['name']} ({result['output_sr']}Hz)")
        return result

    # ---- 策略1: 蓝牙一体（同名设备的 HFP 输入 + Stereo 输出）----
    for hi, hd, hapi, hbt, _ in bt_hfp_inputs:
        for si, sd_dev, sapi, sbt, _ in bt_stereo_outputs:
            if hbt and sbt and hbt == sbt:
                _set_input(hi, hd, hapi)
                _set_output(si, sd_dev, sapi)
                result['bt_name'] = hbt
                result['mode'] = 'bt_unified'
                print(f"[Audio] 蓝牙一体设备: {hbt}")
                print(f"  输入: #{hi} [{hapi}] {hd['name']} ({result['input_sr']}Hz)")
                print(f"  输出: #{si} [{sapi}] {sd_dev['name']} ({result['output_sr']}Hz)")
                return result

    # ---- 策略2: 蓝牙分体（不同蓝牙设备的 HFP + Stereo）----
    if bt_hfp_inputs and bt_stereo_outputs:
        hi, hd, hapi, hbt, _ = bt_hfp_inputs[0]
        si, sd_dev, sapi, sbt, _ = bt_stereo_outputs[0]
        _set_input(hi, hd, hapi)
        _set_output(si, sd_dev, sapi)
        result['bt_name'] = f"{hbt} + {sbt}"
        result['mode'] = 'bt_split'
        print(f"[Audio] 蓝牙分体: 输入={hbt}, 输出={sbt}")
        print(f"  输入: #{hi} [{hapi}] {hd['name']} ({result['input_sr']}Hz)")
        print(f"  输出: #{si} [{sapi}] {sd_dev['name']} ({result['output_sr']}Hz)")
        return result

    # ---- 策略3: 蓝牙+本地混合 ----
    if bt_hfp_inputs and local_outputs:
        hi, hd, hapi, hbt, _ = bt_hfp_inputs[0]
        li, ld, lapi, *_ = local_outputs[0]
        _set_input(hi, hd, hapi)
        _set_output(li, ld, lapi)
        result['bt_name'] = hbt
        result['mode'] = 'bt_mixed'
        print(f"[Audio] 混合: 蓝牙输入={hbt}, 本地输出")
        print(f"  输入: #{hi} [{hapi}] {hd['name']} ({result['input_sr']}Hz)")
        print(f"  输出: #{li} [{lapi}] {ld['name']} ({result['output_sr']}Hz)")
        return result

    if bt_stereo_outputs and local_inputs:
        li, ld, lapi, *_ = local_inputs[0]
        si, sd_dev, sapi, sbt, _ = bt_stereo_outputs[0]
        _set_input(li, ld, lapi)
        _set_output(si, sd_dev, sapi)
        result['bt_name'] = sbt
        result['mode'] = 'bt_mixed'
        print(f"[Audio] 混合: 本地输入, 蓝牙输出={sbt}")
        print(f"  输入: #{li} [{lapi}] {ld['name']} ({result['input_sr']}Hz)")
        print(f"  输出: #{si} [{sapi}] {sd_dev['name']} ({result['output_sr']}Hz)")
        return result

    # ---- 策略4: 纯本地 ----
    if local_inputs and local_outputs:
        li, ld, lapi, *_ = local_inputs[0]
        lo, lod, loapi, *_ = local_outputs[0]
        _set_input(li, ld, lapi)
        _set_output(lo, lod, loapi)
        result['bt_name'] = None
        result['mode'] = 'local'
        print(f"[Audio] 本地设备（未检测到蓝牙）")
        print(f"  输入: #{li} [{lapi}] {ld['name']} ({result['input_sr']}Hz)")
        print(f"  输出: #{lo} [{loapi}] {lod['name']} ({result['output_sr']}Hz)")
        return result

    # ---- 无设备 ----
    raise RuntimeError("未检测到任何可用音频输入/输出设备")


class HFPKeepAlive:
    """HFP SCO 链路保活器 — 持续向 HFP 输出发送静音"""

    def __init__(self, device_id: int, sample_rate: int = 44100):
        self.device_id = device_id
        self.sample_rate = sample_rate
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        time.sleep(0.3)  # 等待 SCO 链路建立

    def _worker(self):
        try:
            stream = sd.OutputStream(
                device=self.device_id,
                samplerate=self.sample_rate,
                channels=1,
                dtype='float32'
            )
            stream.start()
            silence = np.zeros(self.sample_rate // 10, dtype=np.float32)  # 100ms
            while not self._stop.is_set():
                stream.write(silence)
            stream.stop()
            stream.close()
        except Exception as e:
            print(f"[HFPKeepAlive] 错误: {e}")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


class AudioRecorder:
    """蓝牙 HFP 麦克风录音器"""

    def __init__(self, device_id: Optional[int] = None, sample_rate: int = 44100,
                 target_sr: int = 16000, block_size: int = 4410):
        self.device_id = device_id
        self.sample_rate = sample_rate
        self.target_sr = target_sr
        self.block_size = block_size
        self.audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: Optional[sd.InputStream] = None
        self._running = False
        self._callback: Optional[Callable[[np.ndarray], None]] = None

    def _audio_callback(self, indata, frames, time_info, status):
        if status and 'input' not in str(status).lower():
            print(f"[AudioRecorder] Status: {status}")
        audio = indata[:, 0].copy()
        # 重采样到目标采样率 (通常 44100 → 16000)
        if self.sample_rate != self.target_sr:
            audio = fast_resample(audio, self.sample_rate, self.target_sr)
        self.audio_queue.put(audio)
        if self._callback:
            self._callback(audio)

    def start(self, callback: Optional[Callable[[np.ndarray], None]] = None):
        self._callback = callback
        self._running = True
        self._stream = sd.InputStream(
            device=self.device_id,
            samplerate=self.sample_rate,
            channels=1,
            blocksize=self.block_size,
            dtype='float32',
            callback=self._audio_callback
        )
        self._stream.start()
        print(f"[AudioRecorder] 录音启动 (设备={self.device_id}, {self.sample_rate}→{self.target_sr}Hz)")

    def stop(self):
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def get_audio(self, timeout: float = 0.1) -> Optional[np.ndarray]:
        try:
            return self.audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear_queue(self):
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break


class AudioPlayer:
    """音频播放器 — 支持 HFP 和 Stereo 输出"""

    def __init__(self, device_id: Optional[int] = None, sample_rate: int = 44100):
        self.device_id = device_id
        self.sample_rate = sample_rate
        self._playing = False

    def play(self, audio_data: np.ndarray, sample_rate: Optional[int] = None, blocking: bool = True):
        sr = sample_rate or self.sample_rate
        # 重采样到设备采样率
        if sr != self.sample_rate:
            audio_data = fast_resample(audio_data, sr, self.sample_rate)
        self._playing = True
        try:
            sd.play(audio_data, samplerate=self.sample_rate, device=self.device_id)
            if blocking:
                sd.wait()
        finally:
            self._playing = False

    def stop(self):
        sd.stop()
        self._playing = False

    @property
    def is_playing(self) -> bool:
        return self._playing


def check_duplex_support(input_id: int, input_sr: int, output_id: int, output_sr: int,
                         test_duration: float = 1.0) -> dict:
    """检测音频设备是否支持同时输入和输出（全双工）

    实际尝试同时打开输入流和输出流，播放静音同时录音。
    如果能同时工作不报错，就是全双工（duplex）模式；否则半双工。

    返回 dict:
      duplex: bool — 是否支持全双工
      reason: str — 检测结果说明
    """
    import time as _time

    result = {"duplex": False, "reason": ""}

    # 实际测试：同时打开输入流和输出流
    input_ok = False
    output_ok = False
    error_msg = ""

    try:
        # 先开输出流（播放静音）
        out_stream = sd.OutputStream(
            device=output_id,
            samplerate=output_sr,
            channels=1,
            dtype='float32'
        )
        out_stream.start()
        silence = np.zeros(int(output_sr * 0.1), dtype=np.float32)
        out_stream.write(silence)
        output_ok = True

        # 再开输入流（录音）
        recorded = []
        def _cb(indata, frames, time_info, status):
            recorded.append(indata[:, 0].copy())

        in_stream = sd.InputStream(
            device=input_id,
            samplerate=input_sr,
            channels=1,
            dtype='float32',
            callback=_cb
        )
        in_stream.start()
        input_ok = True

        # 同时运行一段时间
        _time.sleep(test_duration)

        # 检查是否都还在运行
        if in_stream.active and out_stream.active and len(recorded) > 0:
            result["duplex"] = True
            result["reason"] = f"设备支持全双工，录到 {len(recorded)} 块音频"
        else:
            result["reason"] = f"流状态异常: in={in_stream.active} out={out_stream.active} blocks={len(recorded)}"

        in_stream.stop()
        in_stream.close()
        out_stream.stop()
        out_stream.close()

    except Exception as e:
        error_msg = str(e)
        result["reason"] = f"全双工测试失败: {error_msg}"
        # 清理
        try:
            if input_ok:
                in_stream.stop()
                in_stream.close()
        except:
            pass
        try:
            if output_ok:
                out_stream.stop()
                out_stream.close()
        except:
            pass

    return result


if __name__ == "__main__":
    print("=== 蓝牙音频设备检测 ===")
    list_devices()
    print()
    input_id, output_id, info = find_bluetooth_devices()
    print(f"蓝牙 HFP 输入: #{input_id} ({info.get('input_name', 'N/A')})")
    print(f"蓝牙 HFP 输出: #{output_id} ({info.get('output_name', 'N/A')})")
    print(f"设备信息: {info}")
