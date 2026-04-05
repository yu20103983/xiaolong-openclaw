"""
TTS 引擎 — edge-tts 文本转语音 + 音频解码 + 磁盘缓存
"""

import asyncio
import edge_tts
import io
import numpy as np
import miniaudio
import threading
import queue
import os
from typing import Optional


class TTSEngine:
    """文本转语音引擎 (基于 edge-tts)"""

    # 推荐中文语音
    VOICES = {
        "xiaoxiao": "zh-CN-XiaoxiaoNeural",   # 女声，温柔
        "yunxi": "zh-CN-YunxiNeural",          # 男声，自然
        "xiaoyi": "zh-CN-XiaoyiNeural",        # 女声，活泼
        "yunjian": "zh-CN-YunjianNeural",       # 男声，沉稳
    }

    # 常用短语预缓存列表
    PRECACHE_PHRASES = [
        "好的", "我在", "好的，我来查一下", "好的，我来处理一下",
        "好的，稍等", "好的，已终止", "我在，请说",
        "语音助手已启动，说小龙小龙唤醒我",
        "好的，再见", "好的，我来看看", "嗯", "好",
        "好的，我来帮你", "好的，马上",
    ]

    # 磁盘缓存目录
    CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache", "tts")

    def __init__(self, voice: str = "xiaoxiao", rate: str = "+0%", volume: str = "+0%"):
        self.voice = self.VOICES.get(voice, voice)
        self.rate = rate
        self.volume = volume
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cache: dict[str, Optional[np.ndarray]] = {}  # TTS音频缓存
        self._cache_lock = threading.Lock()
        self._init_event_loop()
        # 确保磁盘缓存目录存在
        os.makedirs(self.CACHE_DIR, exist_ok=True)

    def _init_event_loop(self):
        """初始化独立的事件循环线程"""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run_async(self, coro):
        """在事件循环中运行协程"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    async def _synthesize_to_bytes(self, text: str) -> bytes:
        """合成语音并返回 mp3 字节"""
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, volume=self.volume)
        audio_bytes = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes += chunk["data"]
        return audio_bytes

    def _cache_file_path(self, text: str) -> str:
        """根据文本+语音+语速生成磁盘缓存文件路径"""
        import hashlib
        key = f"{self.voice}_{self.rate}_{self.volume}_{text}"
        h = hashlib.md5(key.encode('utf-8')).hexdigest()
        return os.path.join(self.CACHE_DIR, f"{h}.npy")

    def _load_from_disk(self, text: str) -> Optional[np.ndarray]:
        """从磁盘加载缓存音频"""
        path = self._cache_file_path(text)
        if os.path.exists(path):
            try:
                return np.load(path)
            except Exception:
                pass
        return None

    def _save_to_disk(self, text: str, audio: np.ndarray):
        """保存音频到磁盘缓存"""
        try:
            path = self._cache_file_path(text)
            np.save(path, audio)
        except Exception as e:
            print(f"[TTS] 磁盘缓存写入失败: {e}")

    def precache(self, phrases: list[str] = None):
        """预缓存常用短语的TTS音频，优先从磁盘加载，未命中再合成并存盘"""
        phrases = phrases or self.PRECACHE_PHRASES
        print(f"[TTS] 预缓存 {len(phrases)} 个常用短语...")

        # 先从磁盘加载
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

        if disk_loaded > 0:
            print(f"[TTS] 从磁盘加载 {disk_loaded} 个")

        if not need_synth:
            print(f"[TTS] 预缓存完成: {disk_loaded}/{len(phrases)} 个(全部命中磁盘)")
            return

        print(f"[TTS] 需合成 {len(need_synth)} 个...")

        def _cache_one(phrase):
            try:
                mp3_data = self._run_async(self._synthesize_to_bytes(phrase))
                if mp3_data:
                    audio = self._decode_mp3(mp3_data)
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
            cached = len([v for v in self._cache.values() if v is not None])
        print(f"[TTS] 预缓存完成: {cached}/{len(phrases)} 个")

    def get_cached(self, text: str) -> Optional[np.ndarray]:
        """查找缓存，命中返回音频，未命中返回None"""
        with self._cache_lock:
            return self._cache.get(text)

    def synthesize(self, text: str, retries: int = 3) -> Optional[np.ndarray]:
        """合成语音，返回 numpy 音频数组 (float32, 24kHz)，优先用缓存，失败自动重试"""
        # 先查缓存
        cached = self.get_cached(text)
        if cached is not None:
            return cached.copy()

        import time as _time
        for attempt in range(retries):
            try:
                mp3_data = self._run_async(self._synthesize_to_bytes(text))
                if mp3_data:
                    audio = self._decode_mp3(mp3_data)
                    if audio is not None:
                        # 缓存合成结果，避免相同文本重复请求
                        with self._cache_lock:
                            if len(self._cache) < 200:  # 限制缓存大小
                                self._cache[text] = audio
                        return audio
                print(f"[TTS] 第{attempt+1}次合成无数据，重试...")
            except Exception as e:
                print(f"[TTS] 第{attempt+1}次合成错误: {e}")
                if 'connect' in str(e).lower() or 'timeout' in str(e).lower():
                    print(f"[TTS] 网络连接异常，请检查网络")
            _time.sleep(0.3 * (attempt + 1))  # 递增等待
        print(f"[TTS] 合成失败，已重试{retries}次: {text[:30]}")
        return None

    def synthesize_to_file(self, text: str, output_path: str) -> bool:
        """合成语音到文件"""
        try:
            mp3_data = self._run_async(self._synthesize_to_bytes(text))
            if mp3_data:
                with open(output_path, 'wb') as f:
                    f.write(mp3_data)
                return True
        except Exception as e:
            print(f"[TTS] 合成到文件错误: {e}")
        return False

    def synthesize_streaming(self, text: str, audio_queue: queue.Queue,
                              done_event: threading.Event):
        """流式合成：边生成边放入队列"""
        def _worker():
            try:
                async def _stream():
                    communicate = edge_tts.Communicate(text, self.voice,
                                                        rate=self.rate, volume=self.volume)
                    buffer = b""
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            buffer += chunk["data"]
                            # 攒够一定量再解码
                            if len(buffer) > 8192:
                                audio = self._decode_mp3(buffer)
                                if audio is not None and len(audio) > 0:
                                    audio_queue.put(audio)
                                buffer = b""
                    # 处理剩余
                    if buffer:
                        audio = self._decode_mp3(buffer)
                        if audio is not None and len(audio) > 0:
                            audio_queue.put(audio)
                    audio_queue.put(None)  # sentinel
                    done_event.set()

                self._run_async(_stream())
            except Exception as e:
                print(f"[TTS] 流式合成错误: {e}")
                done_event.set()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    @staticmethod
    def _decode_mp3(mp3_data: bytes, sample_rate: int = 24000) -> Optional[np.ndarray]:
        """用 miniaudio 解码 MP3 数据为 float32 numpy 数组"""
        try:
            decoded = miniaudio.decode(mp3_data,
                                       output_format=miniaudio.SampleFormat.FLOAT32,
                                       nchannels=1,
                                       sample_rate=sample_rate)
            return np.frombuffer(decoded.samples, dtype=np.float32).copy()
        except Exception as e:
            print(f"[TTS] MP3 解码错误: {e}")
            return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from audio_io import AudioPlayer, find_bluetooth_devices

    print("=== TTS 引擎测试 ===")
    tts = TTSEngine(voice="xiaoxiao")

    text = "你好，我是语音助手小派，很高兴为你服务！"
    print(f"合成: {text}")

    audio = tts.synthesize(text)
    if audio is not None:
        print(f"合成完成: {len(audio)} 样本, {len(audio)/24000:.1f} 秒")

        _, output_id = find_bluetooth_devices()
        player = AudioPlayer(device_id=output_id, sample_rate=24000)
        print("播放中...")
        player.play(audio, blocking=True)
        print("播放完成")
    else:
        print("合成失败")
