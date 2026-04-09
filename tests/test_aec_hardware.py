"""
AEC 硬件回声消除测试（帧同步模式）

核心改进：使用 OutputStream callback 精确同步参考信号，
每输出一帧音频立即 feed 给 AEC，而非一次性预灌。

测试项：
1. 不同滤波器长度对比
2. 帧同步 vs 预灌对比
3. tail_frames 尾部回音消除效果
4. 生成 wav 文件供人工听评
"""

import sys, os, time, threading
import numpy as np
import sounddevice as sd
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from audio_io import fast_resample, auto_detect_devices
from aec_engine import EchoCanceller

# ============ 配置 ============
SAMPLE_RATE = 16000
PLAY_DURATION = 5.0
SILENCE_BEFORE = 1.0
SILENCE_AFTER = 2.0
TOTAL_DURATION = SILENCE_BEFORE + PLAY_DURATION + SILENCE_AFTER
AEC_FRAME_SIZE = 160
AEC_FILTER_LENGTHS = [1600, 3200, 6400, 12800]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'logs')


def generate_test_signal(duration, sr=16000):
    """生成测试信号：正弦波 + 频率扫描"""
    n = int(sr * duration)
    t = np.arange(n, dtype=np.float32) / sr
    half = n // 2
    sig1 = np.sin(2 * np.pi * 440 * t[:half]) * 0.5
    sig1 += np.sin(2 * np.pi * 880 * t[:half]) * 0.25
    freq = np.linspace(200, 2000, n - half)
    phase = np.cumsum(2 * np.pi * freq / sr)
    sig2 = np.sin(phase).astype(np.float32) * 0.5
    return np.concatenate([sig1, sig2]).astype(np.float32)


def save_wav(filename, audio, sr=16000):
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


def record_with_playback(input_id, input_sr, output_id, output_sr, play_audio_data):
    """同时录音+播放（使用 OutputStream callback 精确同步）"""
    recorded_chunks = []
    played_chunks = []
    rec_lock = threading.Lock()
    play_pos = [0]

    def rec_callback(indata, frames, time_info, status):
        with rec_lock:
            recorded_chunks.append(indata[:, 0].copy())

    def play_callback(outdata, frames, time_info, status):
        end = min(play_pos[0] + frames, len(play_audio_data))
        length = end - play_pos[0]
        outdata[:length, 0] = play_audio_data[play_pos[0]:end]
        if length < frames:
            outdata[length:] = 0
        played_chunks.append((play_pos[0], length))
        play_pos[0] = end
        if play_pos[0] >= len(play_audio_data):
            raise sd.CallbackStop()

    print(f"\n  Recording {TOTAL_DURATION:.0f}s...")

    # 启动录音
    rec_stream = sd.InputStream(
        device=input_id, samplerate=input_sr, channels=1,
        dtype='float32', callback=rec_callback,
        blocksize=int(input_sr * 0.01)
    )
    rec_stream.start()
    time.sleep(0.1)

    # 使用 OutputStream callback 播放
    done_event = threading.Event()
    play_stream = sd.OutputStream(
        device=output_id, samplerate=output_sr, channels=1,
        dtype='float32', callback=play_callback,
        blocksize=int(output_sr * 0.01),
        finished_callback=lambda: done_event.set()
    )
    play_stream.start()

    # 等播放完成
    done_event.wait(timeout=TOTAL_DURATION + 5)
    play_stream.close()

    # 继续录 silence_after
    time.sleep(SILENCE_AFTER + 0.5)
    rec_stream.stop()
    rec_stream.close()

    raw_mic = np.concatenate(recorded_chunks)
    if input_sr != SAMPLE_RATE:
        raw_mic = fast_resample(raw_mic, input_sr, SAMPLE_RATE)

    print(f"  Recorded: {len(raw_mic)} samples ({len(raw_mic)/SAMPLE_RATE:.1f}s)")
    print(f"  Played: {len(played_chunks)} callback blocks")
    return raw_mic


def run_hardware_test():
    print("=" * 60)
    print("  AEC Hardware Test (Frame-Sync Mode)")
    print("=" * 60)

    det = auto_detect_devices(prefer_local=True)
    input_id = det['input_id']
    input_sr = det['input_sr']
    output_id = det['output_id']
    output_sr = det['output_sr']
    print(f"\n  Input:  #{input_id} {det['input_name']} ({input_sr}Hz)")
    print(f"  Output: #{output_id} {det['output_name']} ({output_sr}Hz)")

    # 生成信号
    test_signal = generate_test_signal(PLAY_DURATION, SAMPLE_RATE)
    silence_before = np.zeros(int(SAMPLE_RATE * SILENCE_BEFORE), dtype=np.float32)
    silence_after = np.zeros(int(SAMPLE_RATE * SILENCE_AFTER), dtype=np.float32)
    full_reference = np.concatenate([silence_before, test_signal, silence_after])
    play_audio_data = fast_resample(full_reference, SAMPLE_RATE, output_sr)

    # 录制
    raw_mic = record_with_playback(input_id, input_sr, output_id, output_sr, play_audio_data)
    target_len = len(full_reference)
    if len(raw_mic) > target_len:
        raw_mic = raw_mic[:target_len]
    elif len(raw_mic) < target_len:
        raw_mic = np.pad(raw_mic, (0, target_len - len(raw_mic)))

    save_wav('aec_test_reference.wav', full_reference)
    save_wav('aec_test_raw_mic.wav', raw_mic)

    silence_samples = int(SAMPLE_RATE * SILENCE_BEFORE)
    play_samples = int(SAMPLE_RATE * PLAY_DURATION)
    bg_noise = raw_mic[:silence_samples]
    echo_region = raw_mic[silence_samples:silence_samples + play_samples]

    print(f"\n  === Raw Mic ===")
    print(f"  Background RMS: {rms(bg_noise):.6f}")
    print(f"  Echo RMS:       {rms(echo_region):.6f}")
    print(f"  Echo/Noise:     {rms(echo_region)/max(rms(bg_noise),1e-10):.1f}x")

    # ========== 测试1: 不同滤波器长度 ==========
    print(f"\n  === Filter Length Comparison ===")
    best_reduction = 0
    best_filter = 0

    for flen in AEC_FILTER_LENGTHS:
        aec = EchoCanceller(frame_size=AEC_FRAME_SIZE, filter_length=flen,
                            sample_rate=SAMPLE_RATE, tail_frames=0)
        aec.init()

        cleaned = _aec_process_offline(aec, raw_mic, full_reference, AEC_FRAME_SIZE)
        clean_echo = cleaned[silence_samples:silence_samples + play_samples]

        echo_before = rms(echo_region)
        echo_after = rms(clean_echo)
        reduction_db = 20 * np.log10(echo_after / max(echo_before, 1e-10))

        marker = ""
        if abs(reduction_db) > abs(best_reduction):
            best_reduction = reduction_db
            best_filter = flen
            save_wav('aec_test_aec_cleaned.wav', cleaned)
            marker = " <-- best"

        print(f"    filter={flen} ({flen/SAMPLE_RATE*1000:.0f}ms): {reduction_db:+.1f} dB{marker}")

    print(f"\n  Best filter: {best_filter} ({best_reduction:+.1f} dB)")

    # ========== 测试2: tail_frames 尾部回音消除 ==========
    print(f"\n  === Tail Frames Test (filter={best_filter}) ===")
    tail_values = [0, 10, 20, 50, 100]
    # 测量播放结束后0.5秒的残余回音
    tail_start = silence_samples + play_samples
    tail_end = min(tail_start + int(SAMPLE_RATE * 0.5), len(raw_mic))
    raw_tail_rms = rms(raw_mic[tail_start:tail_end])
    print(f"    Raw tail RMS (0.5s after play): {raw_tail_rms:.6f}")

    for tf in tail_values:
        aec = EchoCanceller(frame_size=AEC_FRAME_SIZE, filter_length=best_filter,
                            sample_rate=SAMPLE_RATE, tail_frames=tf)
        aec.init()

        cleaned = _aec_process_offline(aec, raw_mic, full_reference, AEC_FRAME_SIZE)
        clean_tail = cleaned[tail_start:tail_end]
        tail_rms = rms(clean_tail)

        # 也测量播放区域效果
        clean_echo = cleaned[silence_samples:silence_samples + play_samples]
        echo_db = 20 * np.log10(rms(clean_echo) / max(rms(echo_region), 1e-10))

        print(f"    tail={tf:3d}: echo {echo_db:+.1f} dB, tail RMS {tail_rms:.6f}")

    # ========== 测试3: 帧同步 vs 预灌 ==========
    print(f"\n  === Frame-Sync vs Pre-fill (filter={best_filter}) ===")

    # 方式A: 帧同步（逐帧 feed + process）
    aec_sync = EchoCanceller(frame_size=AEC_FRAME_SIZE, filter_length=best_filter,
                              sample_rate=SAMPLE_RATE, tail_frames=20)
    aec_sync.init()
    cleaned_sync = _aec_process_offline(aec_sync, raw_mic, full_reference, AEC_FRAME_SIZE)
    sync_echo = cleaned_sync[silence_samples:silence_samples + play_samples]
    sync_db = 20 * np.log10(rms(sync_echo) / max(rms(echo_region), 1e-10))

    # 方式B: 预灌（先全部 feed reference，再全部 process）
    aec_prefill = EchoCanceller(frame_size=AEC_FRAME_SIZE, filter_length=best_filter,
                                 sample_rate=SAMPLE_RATE, tail_frames=20)
    aec_prefill.init()
    aec_prefill.feed_reference(full_reference)
    cleaned_prefill = aec_prefill.process(raw_mic)
    prefill_echo = cleaned_prefill[silence_samples:silence_samples + play_samples]
    prefill_db = 20 * np.log10(rms(prefill_echo) / max(rms(echo_region), 1e-10))

    print(f"    Frame-sync: {sync_db:+.1f} dB")
    print(f"    Pre-fill:   {prefill_db:+.1f} dB")
    diff = abs(sync_db) - abs(prefill_db)
    if diff > 0:
        print(f"    Frame-sync is {diff:.1f} dB better")
    else:
        print(f"    Pre-fill is {-diff:.1f} dB better (timing not critical for this setup)")

    save_wav('aec_test_sync.wav', cleaned_sync)
    save_wav('aec_test_prefill.wav', cleaned_prefill)

    print(f"\n  Output: {os.path.abspath(OUTPUT_DIR)}")
    print("  Done!")


def _aec_process_offline(aec, raw_mic, full_reference, frame_size):
    """离线逐帧处理：模拟实时帧同步（feed 和 process 交替）"""
    cleaned_chunks = []
    n = len(raw_mic)
    for pos in range(0, n - frame_size + 1, frame_size):
        mic_frame = raw_mic[pos:pos + frame_size]
        ref_frame = full_reference[pos:pos + frame_size]
        aec.feed_reference(ref_frame)
        cleaned = aec.process(mic_frame)
        cleaned_chunks.append(cleaned)
    # 通知播放结束
    aec.on_play_done()
    # 处理剩余（尾部）
    remaining_start = (n // frame_size) * frame_size
    if remaining_start < n:
        tail = raw_mic[remaining_start:]
        cleaned = aec.process(tail)
        cleaned_chunks.append(cleaned)
    return np.concatenate(cleaned_chunks)


if __name__ == "__main__":
    run_hardware_test()
