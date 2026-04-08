"""
回音防护测试 — 验证 mute/unmute 机制正确工作

测试要点:
1. AudioRecorder mute/unmute 状态切换
2. mute 期间音频回调传 None（更新看门狗）但不送入队列
3. unmute 时清空队列残余
4. feed_audio 正确处理 None（只更新看门狗，不送ASR）
"""

import sys
import os
import time
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def test_mute_unmute_basic():
    """基本 mute/unmute 状态测试"""
    from audio_io import AudioRecorder
    
    rec = AudioRecorder(device_id=None, sample_rate=16000, target_sr=16000)
    
    # 初始状态：未静音
    assert not rec.is_muted, "初始状态应该是未静音"
    
    # mute
    rec.mute()
    assert rec.is_muted, "mute 后应为静音状态"
    
    # unmute
    rec.unmute()
    assert not rec.is_muted, "unmute 后应为非静音状态"
    
    # 重复 mute/unmute
    rec.mute()
    rec.mute()  # 重复mute不应出错
    assert rec.is_muted
    rec.unmute()
    assert not rec.is_muted
    
    print("  [OK] mute/unmute 基本状态切换")


def test_muted_callback_sends_none():
    """mute 期间回调应传 None（用于看门狗更新）"""
    from audio_io import AudioRecorder
    
    rec = AudioRecorder(device_id=None, sample_rate=16000, target_sr=16000)
    
    callback_args = []
    rec._callback = lambda data: callback_args.append(data)
    
    # 正常状态：回调收到音频数据
    fake_audio = np.zeros((160, 1), dtype=np.float32)
    rec._audio_callback(fake_audio, 160, None, None)
    assert len(callback_args) == 1
    assert callback_args[0] is not None, "正常状态回调应收到音频数据"
    
    # 清空之前正常测试留下的队列数据
    rec.clear_queue()
    
    # mute 状态：回调收到 None
    callback_args.clear()
    rec.mute()
    rec._audio_callback(fake_audio, 160, None, None)
    assert len(callback_args) == 1
    assert callback_args[0] is None, "mute 状态回调应收到 None"
    
    # 验证 mute 期间音频不进队列
    assert rec.audio_queue.empty(), "mute 期间音频不应进入队列"
    
    # unmute 后恢复正常
    callback_args.clear()
    rec.unmute()
    rec._audio_callback(fake_audio, 160, None, None)
    assert len(callback_args) == 1
    assert callback_args[0] is not None, "unmute 后回调应恢复正常"
    
    print("  [OK] mute 期间回调行为正确")


def test_unmute_clears_queue():
    """unmute 应清空队列中的残余数据"""
    from audio_io import AudioRecorder
    
    rec = AudioRecorder(device_id=None, sample_rate=16000, target_sr=16000)
    
    # 先往队列里塞一些数据
    for _ in range(10):
        rec.audio_queue.put(np.zeros(160, dtype=np.float32))
    
    assert not rec.audio_queue.empty(), "队列应有数据"
    
    # mute 然后 unmute 应清空队列
    rec.mute()
    rec.unmute()
    
    assert rec.audio_queue.empty(), "unmute 后队列应被清空"
    
    print("  [OK] unmute 清空队列")


def test_feed_audio_with_none():
    """feed_audio 收到 None 时应只更新看门狗，不送 ASR"""
    # 模拟 feed_audio 逻辑
    watchdog = {"last_audio": 0}
    asr_fed = []
    
    def feed_audio(data):
        watchdog["last_audio"] = time.time()
        if data is not None:
            asr_fed.append(data)
    
    # None 数据：更新看门狗但不送 ASR
    old_time = watchdog["last_audio"]
    feed_audio(None)
    assert watchdog["last_audio"] > old_time, "看门狗应被更新"
    assert len(asr_fed) == 0, "None 数据不应送入 ASR"
    
    # 正常数据：更新看门狗且送 ASR
    audio = np.zeros(160, dtype=np.float32)
    feed_audio(audio)
    assert len(asr_fed) == 1, "正常数据应送入 ASR"
    
    print("  [OK] feed_audio None 处理正确")


def test_echo_scenario():
    """模拟完整回音场景:
    1. 录音正常 → 音频送入ASR
    2. TTS播放开始 → mute → 音频不送ASR
    3. TTS播放结束 → unmute → 队列清空，恢复正常
    """
    from audio_io import AudioRecorder
    
    rec = AudioRecorder(device_id=None, sample_rate=16000, target_sr=16000)
    
    asr_chunks = []
    watchdog_updates = []
    
    def mock_feed(data):
        watchdog_updates.append(time.time())
        if data is not None:
            asr_chunks.append(data)
    
    rec._callback = mock_feed
    
    fake_audio = np.zeros((160, 1), dtype=np.float32)
    
    # 阶段1：正常录音
    for _ in range(5):
        rec._audio_callback(fake_audio, 160, None, None)
    assert len(asr_chunks) == 5, f"正常阶段应有5个音频块，实际 {len(asr_chunks)}"
    
    # 阶段2：TTS播放 → mute
    asr_chunks.clear()
    watchdog_updates.clear()
    rec.clear_queue()  # 清空阶段1的队列数据
    rec.mute()
    
    # 模拟 TTS 播放期间麦克风捕获回音
    for _ in range(20):  # 大量回音数据
        rec._audio_callback(fake_audio, 160, None, None)
    
    assert len(asr_chunks) == 0, f"mute 期间不应有音频送入ASR，实际 {len(asr_chunks)}"
    assert len(watchdog_updates) == 20, "看门狗应继续更新"
    assert rec.audio_queue.empty(), "mute 期间队列应为空"
    
    # 阶段3：TTS播放结束 → unmute
    rec.unmute()
    asr_chunks.clear()
    
    for _ in range(3):
        rec._audio_callback(fake_audio, 160, None, None)
    assert len(asr_chunks) == 3, f"unmute 后应恢复正常，实际 {len(asr_chunks)}"
    
    print("  [OK] 完整回音场景模拟通过")


if __name__ == "__main__":
    print("=== 回音防护测试 ===\n")
    
    test_mute_unmute_basic()
    test_muted_callback_sends_none()
    test_unmute_clears_queue()
    test_feed_audio_with_none()
    test_echo_scenario()
    
    print("\n=== 全部测试通过 ===")
