"""
测试音频设备检测
运行: set PYTHONIOENCODING=utf-8 && python -X utf8 tests/test_devices.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import sounddevice as sd
from audio_io import auto_detect_devices


def main():
    print("=== 音频设备检测测试 ===\n")

    # 列出所有设备
    print("--- 所有音频设备 ---")
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        hostapi = sd.query_hostapis(d['hostapi'])['name']
        direction = []
        if d['max_input_channels'] > 0:
            direction.append(f"IN×{d['max_input_channels']}")
        if d['max_output_channels'] > 0:
            direction.append(f"OUT×{d['max_output_channels']}")
        print(f"  #{i:2d} [{hostapi}] {d['name']}  {', '.join(direction)}  {int(d['default_samplerate'])}Hz")

    # 自动检测
    print("\n--- 自动检测（含蓝牙）---")
    det = auto_detect_devices(prefer_local=False)
    print(f"  模式: {det.get('mode', '?')}")
    print(f"  输入: #{det['input_id']} {det.get('input_name', '')} ({det['input_sr']}Hz)")
    print(f"  输出: #{det['output_id']} {det.get('output_name', '')} ({det['output_sr']}Hz)")

    print("\n--- 自动检测（仅本地）---")
    det2 = auto_detect_devices(prefer_local=True)
    print(f"  模式: {det2.get('mode', '?')}")
    print(f"  输入: #{det2['input_id']} {det2.get('input_name', '')} ({det2['input_sr']}Hz)")
    print(f"  输出: #{det2['output_id']} {det2.get('output_name', '')} ({det2['output_sr']}Hz)")

    print("\n测试结束")


if __name__ == "__main__":
    main()
