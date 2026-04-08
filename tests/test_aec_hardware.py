"""
实际硬件回声消除测试

流程:
1. 同时启动麦克风录音和扬声器播放
2. 播放一段已知信号(sweep/speech), 麦克风捕获回音
3. 对录到的音频分别做: 无处理 / AEC处理
4. 比较 RMS, 输出 wav 文件供人工听评

输出文件:
  - aec_test_reference.wav   播放的参考信号
  - aec_test_raw_mic.wav     麦克风原始录音(含回音)
  - aec_test_aec_cleaned.wav AEC处理后的音频
"""

import sys, os, time, threading
import numpy as np
import sounddevice as sd
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from audio_io import fast_resample, auto_detect_devices
from aec_engine import EchoCanceller

# ============ 配置 ============
SAMPLE_RATE = 16000       # AEC工作采样率
PLAY_DURATION = 5.0       # 播放时长(秒)
SILENCE_BEFORE = 1.0      # 播放前静音(秒), 录制背景噪音
SILENCE_AFTER = 2.0       # 播放后静音(秒), 观察残余回音
TOTAL_DURATION = SILENCE_BEFORE + PLAY_DURATION + SILENCE_AFTER
AEC_FRAME_SIZE = 160      # 10ms@16kHz
AEC_FILTER_LENGTHS = [1600, 3200, 6400, 12800]  # 测试不同滤波器长度

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'logs')


def generate_test_signal(duration, sr=16000):
    """生成测试信号: 交替的正弦波 + 频率扫描"""
    n = int(sr * duration)
    t = np.arange(n, dtype=np.float32) / sr

    # 前半段: 440Hz + 880Hz 正弦波(模拟TTS的元音)
    half = n // 2
    sig1 = np.sin(2 * np.pi * 440 * t[:half]) * 0.5
    sig1 += np.sin(2 * np.pi * 880 * t[:half]) * 0.25

    # 后半段: 200-2000Hz 线性频率扫描
    freq = np.linspace(200, 2000, n - half)
    phase = np.cumsum(2 * np.pi * freq / sr)
    sig2 = np.sin(phase).astype(np.float32) * 0.5

    signal = np.concatenate([sig1, sig2]).astype(np.float32)
    return signal


def save_wav(filename, audio, sr=16000):
    """保存 float32 音频为 16bit WAV"""
    path = os.path.join(OUTPUT_DIR, filename)
    int16_data = np.clip(audio * 32768, -32768, 32767).astype(np.int16)
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(int16_data.tobytes())
    print(f"  Saved: {path} ({len(audio)/sr:.1f}s)")


def rms(audio):
    return np.sqrt(np.mean(audio.astype(np.float64) ** 2))


def run_hardware_test():
    print("=" * 60)
    print("  AEC Hardware Echo Cancellation Test")
    print("=" * 60)

    # 检测设备
    det = auto_detect_devices(prefer_local=True)
    input_id = det['input_id']
    input_sr = det['input_sr']
    output_id = det['output_id']
    output_sr = det['output_sr']
    print(f"\n  Input:  #{input_id} {det['input_name']} ({input_sr}Hz)")
    print(f"  Output: #{output_id} {det['output_name']} ({output_sr}Hz)")

    # 生成测试信号
    test_signal = generate_test_signal(PLAY_DURATION, SAMPLE_RATE)
    # 加前后静音
    silence_before = np.zeros(int(SAMPLE_RATE * SILENCE_BEFORE), dtype=np.float32)
    silence_after = np.zeros(int(SAMPLE_RATE * SILENCE_AFTER), dtype=np.float32)
    full_reference = np.concatenate([silence_before, test_signal, silence_after])

    # 重采样到播放设备采样率
    play_audio = fast_resample(full_reference, SAMPLE_RATE, output_sr)

    # 录音缓冲区
    recorded_chunks = []
    rec_lock = threading.Lock()
    total_rec_samples = int(input_sr * TOTAL_DURATION) + input_sr  # 多录1秒余量

    def rec_callback(indata, frames, time_info, status):
        if status:
            print(f"  [Rec] {status}")
        with rec_lock:
            recorded_chunks.append(indata[:, 0].copy())

    # ========== 同时录音+播放 ==========
    print(f"\n  Recording {TOTAL_DURATION:.0f}s (silence {SILENCE_BEFORE}s + play {PLAY_DURATION}s + silence {SILENCE_AFTER}s)...")

    rec_stream = sd.InputStream(
        device=input_id, samplerate=input_sr, channels=1,
        dtype='float32', callback=rec_callback,
        blocksize=int(input_sr * 0.01)  # 10ms blocks
    )
    rec_stream.start()
    time.sleep(0.1)  # 让录音先稳定

    # 播放
    sd.play(play_audio, samplerate=output_sr, device=output_id)
    sd.wait()

    # 播放后继续录 silence_after
    time.sleep(SILENCE_AFTER + 0.5)
    rec_stream.stop()
    rec_stream.close()

    # 拼接录音
    raw_mic = np.concatenate(recorded_chunks)
    # 重采样到16kHz
    if input_sr != SAMPLE_RATE:
        raw_mic = fast_resample(raw_mic, input_sr, SAMPLE_RATE)

    # 截取到和参考信号同长
    target_len = len(full_reference)
    if len(raw_mic) > target_len:
        raw_mic = raw_mic[:target_len]
    elif len(raw_mic) < target_len:
        raw_mic = np.pad(raw_mic, (0, target_len - len(raw_mic)))

    print(f"  Recorded: {len(raw_mic)} samples ({len(raw_mic)/SAMPLE_RATE:.1f}s)")

    # 保存原始文件
    save_wav('aec_test_reference.wav', full_reference)
    save_wav('aec_test_raw_mic.wav', raw_mic)

    # ========== 分段 RMS 分析 ==========
    silence_samples = int(SAMPLE_RATE * SILENCE_BEFORE)
    play_samples = int(SAMPLE_RATE * PLAY_DURATION)

    bg_noise = raw_mic[:silence_samples]
    echo_region = raw_mic[silence_samples:silence_samples + play_samples]

    print(f"\n  === Raw Mic Analysis ===")
    print(f"  Background noise RMS: {rms(bg_noise):.6f}")
    print(f"  Echo region RMS:      {rms(echo_region):.6f}")
    print(f"  Echo/Noise ratio:     {rms(echo_region)/max(rms(bg_noise),1e-10):.1f}x")

    # ========== AEC 处理 (不同滤波器长度) ==========
    print(f"\n  === AEC Processing ===")

    best_reduction = 0
    best_filter = 0

    for flen in AEC_FILTER_LENGTHS:
        aec = EchoCanceller(frame_size=AEC_FRAME_SIZE, filter_length=flen,
                            sample_rate=SAMPLE_RATE)
        aec.init()

        # 逐帧处理，模拟实时场景
        cleaned_chunks = []
        frame_size = AEC_FRAME_SIZE
        n = len(raw_mic)

        for pos in range(0, n - frame_size + 1, frame_size):
            mic_frame = raw_mic[pos:pos + frame_size]
            ref_frame = full_reference[pos:pos + frame_size]

            # 先 feed reference, 再 process
            aec.feed_reference(ref_frame)
            cleaned = aec.process(mic_frame)
            cleaned_chunks.append(cleaned)

        cleaned_audio = np.concatenate(cleaned_chunks)

        # 分析AEC效果
        clean_echo_region = cleaned_audio[silence_samples:silence_samples + play_samples]
        clean_bg = cleaned_audio[:silence_samples]

        echo_rms_before = rms(echo_region)
        echo_rms_after = rms(clean_echo_region)
        reduction_db = 20 * np.log10(echo_rms_after / max(echo_rms_before, 1e-10))
        bg_ratio = rms(clean_bg) / max(rms(bg_noise), 1e-10)

        print(f"\n  Filter length={flen} ({flen/SAMPLE_RATE*1000:.0f}ms):")
        print(f"    Echo RMS: {echo_rms_before:.6f} -> {echo_rms_after:.6f} ({reduction_db:+.1f} dB)")
        print(f"    Background preservation: {bg_ratio:.2f}x")

        if abs(reduction_db) > abs(best_reduction):
            best_reduction = reduction_db
            best_filter = flen
            save_wav('aec_test_aec_cleaned.wav', cleaned_audio)

    print(f"\n  === Best: filter_length={best_filter} ({best_reduction:+.1f} dB) ===")

    # ========== 延迟补偿测试 ==========
    print(f"\n  === Delay Compensation Test ===")
    # 测试不同延迟偏移对AEC效果的影响
    delays_ms = [0, 5, 10, 20, 30, 50, 80, 100, 150, 200]
    best_delay = 0
    best_delay_reduction = 0

    for delay_ms in delays_ms:
        delay_samples = int(SAMPLE_RATE * delay_ms / 1000)

        aec = EchoCanceller(frame_size=AEC_FRAME_SIZE, filter_length=best_filter,
                            sample_rate=SAMPLE_RATE)
        aec.init()

        cleaned_chunks = []
        frame_size = AEC_FRAME_SIZE

        for pos in range(0, len(raw_mic) - frame_size + 1, frame_size):
            mic_frame = raw_mic[pos:pos + frame_size]

            # 参考信号加延迟偏移
            ref_pos = pos - delay_samples
            if 0 <= ref_pos and ref_pos + frame_size <= len(full_reference):
                ref_frame = full_reference[ref_pos:ref_pos + frame_size]
                aec.feed_reference(ref_frame)
            # 不在范围内就不feed reference (透传)

            cleaned = aec.process(mic_frame)
            cleaned_chunks.append(cleaned)

        cleaned_audio = np.concatenate(cleaned_chunks)
        clean_echo = cleaned_audio[silence_samples:silence_samples + play_samples]
        echo_rms_after = rms(clean_echo)
        reduction_db = 20 * np.log10(echo_rms_after / max(rms(echo_region), 1e-10))

        marker = " <--" if abs(reduction_db) > abs(best_delay_reduction) else ""
        print(f"    delay={delay_ms:3d}ms: {reduction_db:+.1f} dB{marker}")

        if abs(reduction_db) > abs(best_delay_reduction):
            best_delay_reduction = reduction_db
            best_delay = delay_ms
            save_wav('aec_test_best_delay.wav', cleaned_audio)

    print(f"\n  === Best delay: {best_delay}ms ({best_delay_reduction:+.1f} dB) ===")
    print(f"\n  Output files in: {os.path.abspath(OUTPUT_DIR)}")
    print("  Done!")


if __name__ == "__main__":
    run_hardware_test()
