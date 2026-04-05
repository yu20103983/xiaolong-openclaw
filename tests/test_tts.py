"""
测试 TTS 引擎：合成并播放
运行: set PYTHONIOENCODING=utf-8 && python -X utf8 tests/test_tts.py
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from tts_engine import TTSEngine
from audio_io import auto_detect_devices, fast_resample
import sounddevice as sd


def main():
    print("=== TTS 合成播放测试 ===\n")

    # 自动检测输出设备
    det = auto_detect_devices()
    out_id = det['output_id']
    out_sr = det['output_sr']
    print(f"输出设备: #{out_id} ({out_sr}Hz)\n")

    # 初始化 TTS
    tts = TTSEngine()

    texts = [
        "你好，我是小龙，很高兴为你服务。",
        "今天天气不错，适合出门走走。",
        "好的，我来帮你查一下。",
    ]

    for text in texts:
        print(f"合成: {text}", end=" ... ", flush=True)
        audio = tts.synthesize(text)
        if audio is not None:
            print(f"OK ({len(audio)} samples, {len(audio)/24000:.1f}s)")
            # 重采样到输出设备采样率
            out = fast_resample(audio, 24000, out_sr)
            sd.play(out, samplerate=out_sr, device=out_id)
            sd.wait()
            time.sleep(0.3)
        else:
            print("FAILED")

    print("\n测试结束")


if __name__ == "__main__":
    main()
