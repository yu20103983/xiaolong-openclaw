"""
ASR 引擎 — VAD + SenseVoice 离线识别
用 silero-VAD 检测语音段，用 SenseVoice 做离线识别
比流式 zipformer 准确率高很多
"""

import numpy as np
import os
import threading
import time
import sherpa_onnx
from typing import Optional, Callable
from collections import deque

SENSEVOICE_DIR = os.path.join(os.path.dirname(__file__), "..", "models",
                              "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
VAD_MODEL = os.path.join(os.path.dirname(__file__), "..", "models", "silero_vad.onnx")


class ASREngine:
    """VAD + SenseVoice 离线语音识别引擎
    音频数据通过队列异步送入，VAD + 识别在独立线程执行，不阻塞音频回调"""

    def __init__(self, model_dir: str = SENSEVOICE_DIR, vad_model: str = VAD_MODEL):
        self.model_dir = model_dir
        self.vad_model = vad_model
        self.recognizer: Optional[sherpa_onnx.OfflineRecognizer] = None
        self.vad: Optional[sherpa_onnx.VoiceActivityDetector] = None
        self._on_partial: Optional[Callable[[str], None]] = None
        self._on_final: Optional[Callable[[str], None]] = None
        self._last_text = ""
        self._lock = threading.Lock()
        # VAD 状态
        self._is_speaking = False
        self._speech_buffer = []
        self._silence_after_speech = 0  # 语音后的静音帧数
        # 异步队列：解耦音频回调和识别线程
        self._audio_queue: deque = deque(maxlen=500)  # 环形缓冲区，防止内存爆炸
        self._dropped_chunks = 0  # 丢弃的音频块计数
        self._last_drop_warn = 0  # 上次告警时间
        self._queue_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False

    def init(self):
        """初始化 VAD + SenseVoice 识别器"""
        # 初始化 SenseVoice 离线识别器
        model_path = os.path.join(self.model_dir, "model.onnx")
        tokens_path = os.path.join(self.model_dir, "tokens.txt")

        for f in [model_path, tokens_path]:
            if not os.path.exists(f):
                raise FileNotFoundError(f"模型文件不存在: {f}")

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model_path,
            tokens=tokens_path,
            num_threads=4,
            sample_rate=16000,
            use_itn=True,
            language="zh",
        )

        # 初始化 VAD
        if not os.path.exists(self.vad_model):
            raise FileNotFoundError(f"VAD 模型不存在: {self.vad_model}")

        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = self.vad_model
        vad_config.silero_vad.min_silence_duration = 0.5  # 0.5秒静音视为语音结束
        vad_config.silero_vad.min_speech_duration = 0.25  # 最短语音0.25秒
        vad_config.silero_vad.threshold = 0.5
        vad_config.silero_vad.window_size = 512  # 16kHz下32ms
        vad_config.sample_rate = 16000
        vad_config.num_threads = 2

        self.vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=60)

        print("[ASR] VAD + SenseVoice 引擎初始化完成")
        # 启动异步识别线程
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        print("[ASR] 异步识别线程已启动")

    def set_callbacks(self, on_partial: Optional[Callable[[str], None]] = None,
                      on_final: Optional[Callable[[str], None]] = None):
        self._on_partial = on_partial
        self._on_final = on_final

    def feed_audio(self, samples: np.ndarray):
        """输入音频数据（float32, 16kHz, mono）
        仅入队列，不阻塞音频回调线程"""
        if self.vad is None or self.recognizer is None:
            return
        if len(self._audio_queue) >= self._audio_queue.maxlen:
            self._dropped_chunks += 1
            now = time.time()
            if now - self._last_drop_warn > 5:  # 每5秒最多警告一次
                print(f"[ASR] 警告: 音频队列已满，已丢弃 {self._dropped_chunks} 个音频块")
                self._last_drop_warn = now
        self._audio_queue.append(samples)
        self._queue_event.set()

    def _worker_loop(self):
        """异步识别线程：从队列取音频 → VAD → SenseVoice 识别"""
        while self._running:
            # 等待新音频数据
            self._queue_event.wait(timeout=0.05)
            self._queue_event.clear()

            # 批量取出队列中所有音频块
            chunks = []
            while self._audio_queue:
                try:
                    chunks.append(self._audio_queue.popleft())
                except IndexError:
                    break

            if not chunks:
                continue

            # 拼接后送入 VAD + 识别
            with self._lock:
                for chunk in chunks:
                    self._process_chunk(chunk)

    def _process_chunk(self, samples: np.ndarray):
        """处理一个音频块：VAD + 识别（在 worker 线程中调用，已持锁）"""
        self.vad.accept_waveform(samples)

        while not self.vad.empty():
            speech = self.vad.front
            samples_array = np.array(speech.samples, dtype=np.float32)

            # 用 SenseVoice 识别
            stream = self.recognizer.create_stream()
            stream.accept_waveform(16000, samples_array)
            self.recognizer.decode_stream(stream)
            text = stream.result.text.strip()

            # 清理 SenseVoice 的特殊标记
            text = self._clean_sensevoice_text(text)

            if text:
                self._last_text = text
                if self._on_final:
                    self._on_final(text)

            self.vad.pop()

    @staticmethod
    def _clean_sensevoice_text(text: str) -> str:
        """清理 SenseVoice 输出的特殊标记"""
        import re
        # 移除 <|xx|> 标记 (如 <|zh|>, <|HAPPY|>, <|BGM|> 等)
        text = re.sub(r'<\|[^|]*\|>', '', text)
        return text.strip()

    def reset(self):
        """重置识别状态"""
        with self._lock:
            if self.vad:
                self.vad.reset()
            self._last_text = ""
            self._is_speaking = False
            self._speech_buffer.clear()
        # 清空队列中未处理的音频
        self._audio_queue.clear()
        self._dropped_chunks = 0

    def stop(self):
        """停止异步识别线程"""
        self._running = False
        self._queue_event.set()  # 唤醒线程以便退出
        if self._worker_thread:
            self._worker_thread.join(timeout=3)
            self._worker_thread = None

    def get_current_text(self) -> str:
        return self._last_text


if __name__ == "__main__":
    import sys, io, wave
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    print("=== SenseVoice ASR 测试 ===")
    engine = ASREngine()
    engine.init()

    # 用旧模型的测试 wav
    test_wav = os.path.join(os.path.dirname(__file__), "..", "models",
                            "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
                            "test_wavs", "0.wav")

    with wave.open(test_wav, 'rb') as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        print(f"测试音频: {sr}Hz, {len(audio)/sr:.1f}s")

    results = []
    engine.set_callbacks(
        on_partial=lambda t: print(f"  [部分] {t}"),
        on_final=lambda t: (print(f"  [最终] {t}"), results.append(t))
    )

    # 分块送入
    chunk_size = 512  # VAD window size
    for i in range(0, len(audio), chunk_size):
        chunk = audio[i:i+chunk_size]
        if len(chunk) == chunk_size:
            engine.feed_audio(chunk)

    # 送一些静音触发最后的 VAD
    silence = np.zeros(16000, dtype=np.float32)
    for i in range(0, len(silence), chunk_size):
        engine.feed_audio(silence[i:i+chunk_size])

    print(f"\n结果: {results}")
