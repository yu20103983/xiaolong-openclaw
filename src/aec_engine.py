"""
AEC（声学回音消除）模块 — 基于 pyaec 实现实时回音消除

原理：
  智能音箱式回音消除 —— 已知扬声器播放的信号（参考信号），
  用自适应滤波器从麦克风信号中减去回音分量，保留用户真实语音。

使用方式：
  1. 播放音频时调用 feed_reference() 送入参考信号
  2. 麦克风录音回调中调用 process() 处理录音信号
  3. process() 返回消除回音后的干净音频

依赖：pip install pyaec
"""

import numpy as np
import threading
from collections import deque
from typing import Optional

try:
    from pyaec import Aec
    HAS_PYAEC = True
except ImportError:
    HAS_PYAEC = False
    print("[AEC] pyaec 未安装，回音消除不可用。安装: pip install pyaec")


class EchoCanceller:
    """实时声学回音消除器

    参数:
        frame_size: 每帧样本数 (如 160 = 10ms@16kHz)
        filter_length: 滤波器长度 (越长能消除越长延迟的回音, 默认 6400 = 400ms@16kHz)
        sample_rate: 采样率 (必须与输入音频一致, 通常 16000)
    """

    def __init__(self, frame_size: int = 160, filter_length: int = 6400,
                 sample_rate: int = 16000):
        self.frame_size = frame_size
        self.filter_length = filter_length
        self.sample_rate = sample_rate
        self._aec: Optional[Aec] = None
        self._lock = threading.Lock()

        # 参考信号(扬声器播放)环形缓冲区
        # 存储 int16 样本, 按 frame_size 消费
        self._ref_buffer = deque()
        self._ref_lock = threading.Lock()

        # 统计
        self._frames_processed = 0
        self._enabled = False

    def init(self):
        """初始化 AEC 引擎"""
        if not HAS_PYAEC:
            print("[AEC] pyaec 不可用，跳过初始化")
            return False

        try:
            self._aec = Aec(self.frame_size, self.filter_length, self.sample_rate)
            self._enabled = True
            print(f"[AEC] 回音消除引擎初始化完成 "
                  f"(frame={self.frame_size}, filter={self.filter_length}, "
                  f"sr={self.sample_rate})")
            return True
        except Exception as e:
            print(f"[AEC] 初始化失败: {e}")
            return False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def feed_reference(self, audio_float32: np.ndarray):
        """送入参考信号（扬声器正在播放的音频）

        Args:
            audio_float32: float32 格式音频, 采样率必须为 self.sample_rate
        """
        if not self._enabled:
            return

        # float32 → int16
        int16_data = np.clip(audio_float32 * 32768, -32768, 32767).astype(np.int16)

        with self._ref_lock:
            # 追加到参考缓冲区
            self._ref_buffer.extend(int16_data.tolist())
            # 限制缓冲区大小: 最多存 2 秒参考数据
            max_samples = self.sample_rate * 2
            while len(self._ref_buffer) > max_samples:
                self._ref_buffer.popleft()

    def process(self, rec_float32: np.ndarray) -> np.ndarray:
        """处理麦克风录音，消除回音

        只有当参考缓冲区有数据（正在播放）时才进行 AEC 处理，
        否则直接透传原始音频，避免滤波器破坏正常语音。

        Args:
            rec_float32: float32 格式麦克风录音, 采样率 = self.sample_rate

        Returns:
            消除回音后的 float32 音频（长度与输入相同）
        """
        if not self._enabled:
            return rec_float32

        # 检查参考缓冲区是否有数据（是否正在播放）
        with self._ref_lock:
            has_reference = len(self._ref_buffer) >= self.frame_size

        if not has_reference:
            # 没有播放，直接透传，不经过滤波器
            return rec_float32

        # float32 → int16
        rec_int16 = np.clip(rec_float32 * 32768, -32768, 32767).astype(np.int16)

        # 按 frame_size 分帧处理
        n_samples = len(rec_int16)
        output_samples = []
        pos = 0

        while pos + self.frame_size <= n_samples:
            rec_frame = rec_int16[pos:pos + self.frame_size]

            # 取参考帧，不足则透传
            ref_frame = self._get_ref_frame()
            if ref_frame is None:
                # 参考缓冲区用完，剩余帧直接透传
                output_samples.append(rec_int16[pos:])
                pos = n_samples
                break

            # AEC 处理：传 list of int16（不是 bytes）
            with self._lock:
                cleaned_list = self._aec.cancel_echo(
                    rec_frame.tolist(), ref_frame.tolist()
                )

            # pyaec 返回 list of int16 样本
            cleaned_int16 = np.array(cleaned_list, dtype=np.int16)
            output_samples.append(cleaned_int16)
            pos += self.frame_size

            self._frames_processed += 1

        # 处理尾部不足一帧的部分（直接透传）
        if pos < n_samples:
            output_samples.append(rec_int16[pos:])

        # int16 → float32
        result = np.concatenate(output_samples).astype(np.float32) / 32768.0
        return result

    def _get_ref_frame(self):
        """从参考缓冲区取一帧。不足一帧则返回 None（表示无播放，应透传）"""
        with self._ref_lock:
            if len(self._ref_buffer) >= self.frame_size:
                frame = np.array(
                    [self._ref_buffer.popleft() for _ in range(self.frame_size)],
                    dtype=np.int16
                )
                return frame
            else:
                # 参考缓冲区不足 → 无播放，返回 None
                # 清空残余部分数据
                self._ref_buffer.clear()
                return None

    def reset(self):
        """重置 AEC 状态（清空参考缓冲区，重建滤波器）"""
        with self._ref_lock:
            self._ref_buffer.clear()
        if self._enabled:
            with self._lock:
                try:
                    self._aec = Aec(self.frame_size, self.filter_length,
                                    self.sample_rate)
                except Exception as e:
                    print(f"[AEC] 重置失败: {e}")

    def clear_reference(self):
        """清空参考缓冲区（不重置滤波器）"""
        with self._ref_lock:
            self._ref_buffer.clear()

    @property
    def stats(self) -> dict:
        return {
            "enabled": self._enabled,
            "frames_processed": self._frames_processed,
            "ref_buffer_samples": len(self._ref_buffer),
        }


if __name__ == "__main__":
    """AEC 基础测试"""
    print("=== AEC 回音消除测试 ===\n")

    if not HAS_PYAEC:
        print("pyaec 未安装, 跳过测试")
        exit(1)

    import time

    aec = EchoCanceller(frame_size=160, filter_length=3200, sample_rate=16000)
    assert aec.init(), "初始化失败"

    # 生成测试信号
    duration = 1.0  # 1秒
    sr = 16000
    t = np.arange(int(sr * duration), dtype=np.float32) / sr

    # 扬声器播放 440Hz 音调
    speaker = np.sin(2 * np.pi * 440 * t).astype(np.float32) * 0.8

    # 用户语音 200Hz (较低频率, 模拟人声)
    user_voice = np.sin(2 * np.pi * 200 * t).astype(np.float32) * 0.3

    # 麦克风 = 回音(扬声器*0.3) + 用户语音 + 噪音
    echo = speaker * 0.3
    noise = np.random.randn(len(t)).astype(np.float32) * 0.005
    mic = echo + user_voice + noise

    # 先送参考信号
    aec.feed_reference(speaker)

    # 处理麦克风信号
    start = time.perf_counter()
    cleaned = aec.process(mic)
    elapsed = time.perf_counter() - start

    # 计算 RMS
    echo_rms = np.sqrt(np.mean(echo ** 2))
    mic_rms = np.sqrt(np.mean(mic ** 2))
    cleaned_rms = np.sqrt(np.mean(cleaned ** 2))
    user_rms = np.sqrt(np.mean(user_voice ** 2))

    print(f"回音 RMS:     {echo_rms:.4f}")
    print(f"用户语音 RMS: {user_rms:.4f}")
    print(f"麦克风 RMS:   {mic_rms:.4f} (回音+语音+噪音)")
    print(f"消除后 RMS:   {cleaned_rms:.4f}")
    print(f"处理耗时:     {elapsed*1000:.1f}ms ({len(t)/sr:.1f}s 音频)")
    print(f"实时率:       {elapsed/(len(t)/sr)*100:.1f}% CPU")
    print(f"已处理帧数:   {aec.stats['frames_processed']}")

    # 基本断言
    assert cleaned_rms < mic_rms, "消除后 RMS 应该小于原始信号"
    print("\n=== 测试通过 ===")
