"""
测试 ASR 引擎：麦克风实时识别
运行: set PYTHONIOENCODING=utf-8 && python -X utf8 tests/test_asr.py
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from asr_engine import ASREngine
from audio_io import auto_detect_devices, AudioRecorder


def main():
    print("=== ASR 实时识别测试 ===\n")

    # 自动检测音频设备
    det = auto_detect_devices()
    dev_id = det['input_id']
    dev_sr = det['input_sr']
    print(f"输入设备: #{dev_id} ({dev_sr}Hz)\n")

    # 初始化 ASR
    asr = ASREngine()

    def on_final(text):
        print(f"  [识别] {text}")

    asr.set_callbacks(on_final=on_final)
    asr.init()

    # 开始录音
    recorder = AudioRecorder(device_id=dev_id, sample_rate=dev_sr,
                             target_sr=16000, block_size=dev_sr // 10)
    recorder.start(callback=lambda data: asr.feed_audio(data))

    print("正在录音，请对麦克风说话... (Ctrl+C 退出)\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    recorder.stop()
    asr.stop()
    print("\n测试结束")


if __name__ == "__main__":
    main()
