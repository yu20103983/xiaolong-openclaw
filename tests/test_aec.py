"""
AEC 集成测试 — 验证声学回音消除在音频管线中正确工作

测试要点:
1. AEC 引擎基础功能: 初始化、参考信号送入、回音消除
2. 模拟完整管线: 播放TTS → 麦克风捕获回音+用户语音 → AEC消除回音 → ASR
3. 性能测试: 处理延迟在实时范围内
4. 降级测试: pyaec 不可用时正常降级
"""

import sys
import os
import time
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def test_aec_basic():
    """AEC 基本功能测试"""
    from aec_engine import EchoCanceller, HAS_PYAEC
    if not HAS_PYAEC:
        print("  [SKIP] pyaec not installed")
        return

    aec = EchoCanceller(frame_size=160, filter_length=3200, sample_rate=16000)
    assert aec.init(), "AEC init failed"
    assert aec.enabled, "AEC should be enabled"
    assert aec.stats["frames_processed"] == 0

    # 纯静音处理
    silence = np.zeros(160, dtype=np.float32)
    result = silence_out = aec.process(silence)
    assert len(result) == 160, f"Output length mismatch: {len(result)}"

    print("  [OK] AEC 基础功能")


def test_aec_echo_reduction():
    """验证 AEC 能有效减少回音"""
    from aec_engine import EchoCanceller, HAS_PYAEC
    if not HAS_PYAEC:
        print("  [SKIP] pyaec not installed")
        return

    aec = EchoCanceller(frame_size=160, filter_length=3200, sample_rate=16000)
    assert aec.init()

    sr = 16000
    duration = 0.5  # 500ms
    n = int(sr * duration)
    t = np.arange(n, dtype=np.float32) / sr

    # 扬声器信号: 440Hz 正弦波
    speaker = (np.sin(2 * np.pi * 440 * t) * 0.8).astype(np.float32)
    # 麦克风捕获的回音: 扬声器信号衰减后
    echo = speaker * 0.3

    # Phase 1: 送入参考信号并处理 (让滤波器收敛)
    aec.feed_reference(speaker)
    cleaned = aec.process(echo)

    echo_rms = np.sqrt(np.mean(echo ** 2))
    clean_rms = np.sqrt(np.mean(cleaned ** 2))

    # 回音应该被明显减少
    reduction_db = 20 * np.log10(clean_rms / echo_rms) if clean_rms > 0 else -100
    print(f"  [INFO] Echo RMS: {echo_rms:.4f} -> Cleaned RMS: {clean_rms:.4f} ({reduction_db:.1f} dB)")
    assert clean_rms < echo_rms, f"Cleaned should be quieter: {clean_rms} >= {echo_rms}"

    print("  [OK] AEC 回音抑制有效")


def test_aec_preserves_speech():
    """验证 AEC 保留用户语音（不播放时）"""
    from aec_engine import EchoCanceller, HAS_PYAEC
    if not HAS_PYAEC:
        print("  [SKIP] pyaec not installed")
        return

    aec = EchoCanceller(frame_size=160, filter_length=3200, sample_rate=16000)
    assert aec.init()

    sr = 16000
    n = int(sr * 0.5)
    t = np.arange(n, dtype=np.float32) / sr

    # 无播放（无参考信号）时用户说话
    user_voice = (np.sin(2 * np.pi * 200 * t) * 0.5).astype(np.float32)
    cleaned = aec.process(user_voice)

    voice_rms = np.sqrt(np.mean(user_voice ** 2))
    clean_rms = np.sqrt(np.mean(cleaned ** 2))

    # 无回音时应基本保留用户语音 (允许轻微衰减)
    ratio = clean_rms / voice_rms
    print(f"  [INFO] Voice RMS: {voice_rms:.4f} -> Cleaned: {clean_rms:.4f} (ratio: {ratio:.2f})")
    assert ratio > 0.3, f"User voice too attenuated: ratio={ratio:.2f}"

    print("  [OK] AEC 保留用户语音")


def test_aec_performance():
    """AEC 性能测试: 处理速度必须快于实时"""
    from aec_engine import EchoCanceller, HAS_PYAEC
    if not HAS_PYAEC:
        print("  [SKIP] pyaec not installed")
        return

    aec = EchoCanceller(frame_size=160, filter_length=3200, sample_rate=16000)
    assert aec.init()

    # 准备 1 秒测试数据
    audio = np.random.randn(16000).astype(np.float32) * 0.1
    ref = np.random.randn(16000).astype(np.float32) * 0.1

    aec.feed_reference(ref)

    start = time.perf_counter()
    for _ in range(10):  # 10秒音频
        aec.feed_reference(ref)
        aec.process(audio)
    elapsed = time.perf_counter() - start

    cpu_pct = elapsed / 10 * 100
    print(f"  [INFO] 10s audio processed in {elapsed*1000:.0f}ms ({cpu_pct:.1f}% CPU)")
    assert cpu_pct < 50, f"AEC too slow: {cpu_pct:.1f}% CPU"

    print("  [OK] AEC 性能合格")


def test_aec_reset():
    """AEC 重置测试"""
    from aec_engine import EchoCanceller, HAS_PYAEC
    if not HAS_PYAEC:
        print("  [SKIP] pyaec not installed")
        return

    aec = EchoCanceller(frame_size=160, filter_length=3200, sample_rate=16000)
    assert aec.init()

    # 送入一些参考信号
    ref = np.random.randn(16000).astype(np.float32) * 0.1
    aec.feed_reference(ref)
    assert aec.stats["ref_buffer_samples"] > 0

    # 重置
    aec.reset()
    assert aec.stats["ref_buffer_samples"] == 0
    assert aec.enabled  # 重置后仍然可用

    # 重置后仍能正常工作
    audio = np.zeros(160, dtype=np.float32)
    result = aec.process(audio)
    assert len(result) == 160

    print("  [OK] AEC 重置功能")


def test_aec_disabled_passthrough():
    """AEC 未初始化时应直接透传音频"""
    from aec_engine import EchoCanceller

    aec = EchoCanceller()
    # 不调用 init()

    audio = np.random.randn(160).astype(np.float32)
    result = aec.process(audio)
    np.testing.assert_array_equal(result, audio)

    # feed_reference 不应报错
    aec.feed_reference(audio)

    print("  [OK] AEC 未启用时透传")


def test_pipeline_simulation():
    """模拟完整音频管线:
    TTS播放 → 麦克风(回音+用户语音) → AEC → 验证输出
    """
    from aec_engine import EchoCanceller, HAS_PYAEC
    if not HAS_PYAEC:
        print("  [SKIP] pyaec not installed")
        return

    aec = EchoCanceller(frame_size=160, filter_length=3200, sample_rate=16000)
    assert aec.init()

    sr = 16000
    frame_size = 160  # 10ms

    # 模拟 2 秒场景:
    # 0-1s: TTS播放（有回音，无用户语音）
    # 1-2s: TTS播放 + 用户说话（有回音+语音）
    total_frames = 200  # 2s

    # TTS播放信号 (全程 440Hz)
    tts_signal = np.sin(2 * np.pi * 440 * np.arange(sr * 2, dtype=np.float32) / sr) * 0.8

    # 用户语音 (仅后半段 200Hz)
    user_signal = np.zeros(sr * 2, dtype=np.float32)
    user_signal[sr:] = np.sin(2 * np.pi * 200 * np.arange(sr, dtype=np.float32) / sr) * 0.4

    # 模拟帧处理
    echo_only_rms = []
    speech_mixed_rms = []
    cleaned_echo_rms = []
    cleaned_speech_rms = []

    for i in range(total_frames):
        start = i * frame_size
        end = start + frame_size

        # 参考信号(扬声器播放)
        ref_frame = tts_signal[start:end]
        aec.feed_reference(ref_frame)

        # 麦克风信号 = 回音 + 用户语音
        echo = ref_frame * 0.3
        voice = user_signal[start:end]
        mic_frame = echo + voice

        # AEC 处理
        cleaned = aec.process(mic_frame)

        if i < 100:  # 前 1 秒: 只有回音
            echo_only_rms.append(np.sqrt(np.mean(mic_frame ** 2)))
            cleaned_echo_rms.append(np.sqrt(np.mean(cleaned ** 2)))
        else:  # 后 1 秒: 回音 + 语音
            speech_mixed_rms.append(np.sqrt(np.mean(mic_frame ** 2)))
            cleaned_speech_rms.append(np.sqrt(np.mean(cleaned ** 2)))

    avg_echo = np.mean(echo_only_rms)
    avg_cleaned_echo = np.mean(cleaned_echo_rms)
    avg_mixed = np.mean(speech_mixed_rms)
    avg_cleaned_speech = np.mean(cleaned_speech_rms)

    print(f"  [INFO] Echo only: {avg_echo:.4f} -> {avg_cleaned_echo:.4f}")
    print(f"  [INFO] Echo+Speech: {avg_mixed:.4f} -> {avg_cleaned_speech:.4f}")

    # 纯回音应该被大幅减少
    assert avg_cleaned_echo < avg_echo * 0.5, \
        f"Echo not sufficiently reduced: {avg_cleaned_echo:.4f} >= {avg_echo * 0.5:.4f}"

    print("  [OK] 完整管线模拟通过")


if __name__ == "__main__":
    print("=== AEC 集成测试 ===\n")

    test_aec_basic()
    test_aec_echo_reduction()
    test_aec_preserves_speech()
    test_aec_performance()
    test_aec_reset()
    test_aec_disabled_passthrough()
    test_pipeline_simulation()

    print("\n=== 全部测试通过 ===")
