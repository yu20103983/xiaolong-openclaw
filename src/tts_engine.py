"""TTS 引擎 — edge-tts (默认) + Matcha-TTS 本地备选 + SAPI 保底

合成优先级:
  edge-tts  → 在线微软语音，音质好，需联网
  Matcha-TTS → sherpa-onnx 本地推理，离线可用
  SAPI      → Windows 内置语音，最后保底
"""

import os
import time
import hashlib
import asyncio
import threading
import subprocess
import tempfile
import numpy as np
from typing import Optional

# edge-tts (可选依赖)
try:
    import edge_tts
    _HAS_EDGE_TTS = True
except ImportError:
    _HAS_EDGE_TTS = False

# miniaudio (MP3 解码)
try:
    import miniaudio
    _HAS_MINIAUDIO = True
except ImportError:
    _HAS_MINIAUDIO = False

# sherpa-onnx (可选依赖)
try:
    import sherpa_onnx
    _HAS_SHERPA = True
except ImportError:
    _HAS_SHERPA = False


# ============ edge-tts 语音映射 ============
EDGE_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",   # 女声，温柔
    "yunxi":    "zh-CN-YunxiNeural",       # 男声，自然
    "xiaoyi":   "zh-CN-XiaoyiNeural",      # 女声，活泼
    "yunjian":  "zh-CN-YunjianNeural",     # 男声，沉稳
}

# ============ 本地模型配置 ============
LOCAL_MODELS = {
    "matcha-zh-baker": {
        "type": "matcha",
        "dir": "matcha-icefall-zh-baker",
        "acoustic_model": "model-steps-3.onnx",
        "vocoder": "hifigan_v2.onnx",
        "lexicon": "lexicon.txt",
        "tokens": "tokens.txt",
        "dict_dir": "dict",
        "description": "中文女声 (Baker, 22050Hz)",
    },
}


class TTSEngine:
    """文本转语音引擎

    支持三种后端:
      - edge-tts: 微软在线语音 (默认, 音质最好)
      - local: sherpa-onnx 本地推理 (离线, 低延迟)
      - sapi: Windows SAPI (保底)

    edge-tts 失败时自动回退到本地模型，本地也失败则用 SAPI。
    """

    OUTPUT_SR = 24000  # 统一输出采样率

    PRECACHE_PHRASES = [
        "好的", "我在", "好的，我来查一下", "好的，我来处理一下",
        "好的，稍等", "好的，已终止", "我在，请说",
        "语音助手已启动，说小龙小龙唤醒我",
        "好的，再见", "好的，我来看看", "嗯", "好",
        "好的，我来帮你", "好的，马上",
        "连续对话已结束，需要时再叫我",
    ]

    CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache", "tts")

    def __init__(self, engine: str = "edge", voice: str = "xiaoxiao",
                 rate: str = "+10%", local_model: str = "matcha-zh-baker",
                 models_dir: str = None):
        self.engine = engine
        self.voice = voice
        self.rate = rate
        self.local_model = local_model
        self._models_dir = models_dir or os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "models"
        )
        # 内部状态
        self._local_tts = None
        self._local_sr = 24000
        self._edge_loop = None
        self._edge_thread = None
        self._edge_fail_count = 0
        self._edge_max_fails = 3  # 连续失败 N 次后自动切换本地
        self._cache: dict[str, Optional[np.ndarray]] = {}
        self._cache_lock = threading.Lock()

        os.makedirs(self.CACHE_DIR, exist_ok=True)

        # 初始化
        if self.engine == "edge" and _HAS_EDGE_TTS:
            self._init_edge_loop()
            print(f"[TTS] 引擎: edge-tts ({EDGE_VOICES.get(voice, voice)})")
        elif self.engine == "local":
            self._init_local()
            print(f"[TTS] 引擎: 本地 ({self.local_model})")
        else:
            if self.engine == "edge" and not _HAS_EDGE_TTS:
                print("[TTS] edge-tts 未安装，回退到本地模型")
            self._init_local()

        # 后台预加载本地备选（edge 模式下异步加载，不阻塞启动）
        if self.engine == "edge" and _HAS_SHERPA:
            threading.Thread(target=self._init_local, daemon=True).start()

    @property
    def sample_rate(self) -> int:
        return self.OUTPUT_SR

    # ==================== edge-tts ====================

    def _init_edge_loop(self):
        """创建独立事件循环线程"""
        self._edge_loop = asyncio.new_event_loop()
        self._edge_thread = threading.Thread(
            target=self._edge_loop.run_forever, daemon=True
        )
        self._edge_thread.start()

    def _edge_synthesize(self, text: str) -> Optional[np.ndarray]:
        """edge-tts 在线合成"""
        if not _HAS_EDGE_TTS or self._edge_loop is None:
            return None
        if self._edge_fail_count > self._edge_max_fails:
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._edge_generate(text), self._edge_loop
            )
            audio = future.result(timeout=15)
            if audio is not None:
                self._edge_fail_count = 0
                return audio
        except Exception as e:
            self._edge_fail_count += 1
            if self._edge_fail_count <= self._edge_max_fails:
                print(f"[TTS] edge-tts 失败({self._edge_fail_count}): {e}")
            elif self._edge_fail_count == self._edge_max_fails + 1:
                print(f"[TTS] edge-tts 连续失败{self._edge_max_fails}次，自动切换本地")
        return None

    async def _edge_generate(self, text: str) -> Optional[np.ndarray]:
        """异步 edge-tts 合成"""
        voice_id = EDGE_VOICES.get(self.voice, self.voice)
        communicate = edge_tts.Communicate(text, voice_id, rate=self.rate)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        if not chunks:
            return None
        mp3_data = b"".join(chunks)
        return self._decode_mp3(mp3_data)

    @staticmethod
    def _decode_mp3(mp3_data: bytes) -> Optional[np.ndarray]:
        """MP3 → float32 numpy (24kHz)"""
        if not _HAS_MINIAUDIO:
            return None
        try:
            decoded = miniaudio.decode(
                mp3_data,
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=1,
                sample_rate=24000
            )
            return np.frombuffer(decoded.samples, dtype=np.float32).copy()
        except Exception as e:
            print(f"[TTS] MP3 解码失败: {e}")
            return None

    # ==================== 本地 Matcha-TTS ====================

    def _init_local(self):
        """初始化本地 sherpa-onnx TTS"""
        if self._local_tts is not None:
            return
        if not _HAS_SHERPA:
            return

        cfg = LOCAL_MODELS.get(self.local_model)
        if cfg is None:
            return

        model_dir = os.path.join(self._models_dir, cfg["dir"])
        if not os.path.isdir(model_dir):
            return

        try:
            if cfg["type"] == "matcha":
                config = sherpa_onnx.OfflineTtsConfig(
                    model=sherpa_onnx.OfflineTtsModelConfig(
                        matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                            acoustic_model=os.path.join(model_dir, cfg["acoustic_model"]),
                            vocoder=os.path.join(model_dir, cfg["vocoder"]),
                            lexicon=os.path.join(model_dir, cfg["lexicon"]),
                            tokens=os.path.join(model_dir, cfg["tokens"]),
                            dict_dir=os.path.join(model_dir, cfg.get("dict_dir", "")),
                            length_scale=1.0,
                        ),
                        num_threads=4,
                    ),
                )
            else:
                return

            self._local_tts = sherpa_onnx.OfflineTts(config)
            self._local_sr = self._local_tts.sample_rate
            print(f"[TTS] 本地备选已加载: {cfg['description']}")
        except Exception as e:
            print(f"[TTS] 本地模型加载失败: {e}")

    def _local_synthesize(self, text: str) -> Optional[np.ndarray]:
        """本地合成"""
        if self._local_tts is None:
            self._init_local()
        if self._local_tts is None:
            return None
        try:
            audio = self._local_tts.generate(text, sid=0, speed=1.0)
            if audio and audio.samples:
                samples = np.array(audio.samples, dtype=np.float32)
                if self._local_sr != self.OUTPUT_SR:
                    from audio_io import fast_resample
                    samples = fast_resample(samples, self._local_sr, self.OUTPUT_SR)
                return samples
        except Exception as e:
            print(f"[TTS] 本地合成错误: {e}")
        return None

    # ==================== SAPI 保底 ====================

    def _fallback_sapi(self, text: str) -> Optional[np.ndarray]:
        """Windows SAPI 保底"""
        tmp_wav = tmp_ps1 = None
        try:
            tmp_fd, tmp_wav = tempfile.mkstemp(suffix='.wav')
            os.close(tmp_fd)
            tmp_ps1 = tmp_wav.replace('.wav', '.ps1')
            safe_text = text.replace('"', "'").replace('\n', ' ')
            with open(tmp_ps1, 'w', encoding='utf-8-sig') as f:
                f.write('Add-Type -AssemblyName System.Speech\n')
                f.write('$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n')
                f.write('$synth.Rate = 2\n')
                f.write(f'$synth.SetOutputToWaveFile("{tmp_wav}")\n')
                f.write(f'$synth.Speak("{safe_text}")\n')
                f.write('$synth.Dispose()\n')
            result = subprocess.run(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', tmp_ps1],
                capture_output=True, timeout=15
            )
            if result.returncode != 0:
                return None
            if not os.path.exists(tmp_wav) or os.path.getsize(tmp_wav) < 100:
                return None
            return self._decode_wav(tmp_wav, self.OUTPUT_SR)
        except Exception as e:
            print(f"[TTS-SAPI] 失败: {e}")
            return None
        finally:
            for f in (tmp_wav, tmp_ps1):
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

    @staticmethod
    def _decode_wav(wav_path: str, target_sr: int = 24000) -> Optional[np.ndarray]:
        """WAV → float32 numpy + 重采样"""
        try:
            import wave
            with wave.open(wav_path, 'rb') as wf:
                n_ch = wf.getnchannels()
                sw = wf.getsampwidth()
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            if sw == 2:
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            elif sw == 1:
                samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
            else:
                return None
            if n_ch > 1:
                samples = samples[::n_ch]
            if sr != target_sr:
                n = int(len(samples) / sr * target_sr)
                samples = np.interp(
                    np.linspace(0, len(samples) - 1, n),
                    np.arange(len(samples)), samples
                ).astype(np.float32)
            return samples
        except Exception:
            return None

    # ==================== 统一合成接口 ====================

    def _do_synthesize(self, text: str) -> Optional[np.ndarray]:
        """按优先级尝试各引擎合成"""
        if self.engine == "edge":
            # edge-tts → 本地备选 → SAPI
            audio = self._edge_synthesize(text)
            if audio is not None:
                return audio
            audio = self._local_synthesize(text)
            if audio is not None:
                return audio
        else:
            # 本地优先
            audio = self._local_synthesize(text)
            if audio is not None:
                return audio

        return self._fallback_sapi(text)

    # ==================== 缓存 ====================

    def _cache_key(self, text: str) -> str:
        key = f"{self.engine}_{self.voice}_{self.rate}_{text}"
        return hashlib.md5(key.encode('utf-8')).hexdigest()

    def _cache_file(self, text: str) -> str:
        return os.path.join(self.CACHE_DIR, f"{self._cache_key(text)}.npy")

    def _load_from_disk(self, text: str) -> Optional[np.ndarray]:
        path = self._cache_file(text)
        if os.path.exists(path):
            try:
                return np.load(path)
            except Exception:
                pass
        return None

    def _save_to_disk(self, text: str, audio: np.ndarray):
        try:
            np.save(self._cache_file(text), audio)
        except Exception:
            pass

    def get_cached(self, text: str) -> Optional[np.ndarray]:
        with self._cache_lock:
            return self._cache.get(text)

    # ==================== 公开 API ====================

    def synthesize(self, text: str, retries: int = 2) -> Optional[np.ndarray]:
        """合成语音，返回 float32 numpy (24kHz)

        优先查内存/磁盘缓存，未命中按 edge→local→SAPI 顺序合成。
        """
        # 内存缓存
        cached = self.get_cached(text)
        if cached is not None:
            return cached.copy()

        # 磁盘缓存
        disk = self._load_from_disk(text)
        if disk is not None:
            with self._cache_lock:
                if len(self._cache) < 200:
                    self._cache[text] = disk
            return disk.copy()

        for attempt in range(retries):
            audio = self._do_synthesize(text)
            if audio is not None and len(audio) > 0:
                with self._cache_lock:
                    if len(self._cache) < 200:
                        self._cache[text] = audio
                self._save_to_disk(text, audio)
                return audio
            if attempt < retries - 1:
                time.sleep(0.2)

        return None

    def precache(self, phrases: list[str] = None):
        """预缓存常用短语"""
        phrases = phrases or self.PRECACHE_PHRASES
        print(f"[TTS] 预缓存 {len(phrases)} 个常用短语...")

        need_synth = []
        disk_loaded = 0
        for phrase in phrases:
            audio = self._load_from_disk(phrase)
            if audio is not None:
                with self._cache_lock:
                    self._cache[phrase] = audio
                disk_loaded += 1
            else:
                need_synth.append(phrase)

        if disk_loaded:
            print(f"[TTS] 从磁盘加载 {disk_loaded} 个")

        if not need_synth:
            print(f"[TTS] 预缓存完成: {disk_loaded}/{len(phrases)} 个(全部命中磁盘)")
            return

        print(f"[TTS] 需合成 {len(need_synth)} 个...")

        def _cache_one(phrase):
            try:
                audio = self._do_synthesize(phrase)
                if audio is not None:
                    with self._cache_lock:
                        self._cache[phrase] = audio
                    self._save_to_disk(phrase, audio)
            except Exception as e:
                print(f"[TTS] 预缓存失败 '{phrase}': {e}")

        threads = []
        for phrase in need_synth:
            t = threading.Thread(target=_cache_one, args=(phrase,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=15)

        with self._cache_lock:
            cached = sum(1 for v in self._cache.values() if v is not None)
        print(f"[TTS] 预缓存完成: {cached}/{len(phrases)} 个")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))

    print("=== TTS 引擎测试 ===")
    tts = TTSEngine(voice="xiaoxiao")

    text = "你好，我是小龙，很高兴为你服务。"
    print(f"合成: {text}")

    audio = tts.synthesize(text)
    if audio is not None:
        print(f"合成完成: {len(audio)} 样本, {len(audio)/24000:.1f} 秒")
        import sounddevice as sd
        sd.play(audio, samplerate=24000)
        sd.wait()
        print("播放完成")
    else:
        print("合成失败")
