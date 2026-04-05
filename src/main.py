"""
小龙语音助手 - 主程序

功能:语音唤醒、指令识别、Agent 交互、流式 TTS 播报、语音打断
"""
import sys, os, time, threading, re, queue
import numpy as np
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.path.insert(0, os.path.dirname(__file__))

import sounddevice as sd
from audio_io import AudioRecorder, auto_detect_devices, check_duplex_support, fast_resample
from asr_engine import ASREngine
from tts_engine import TTSEngine
from agent_client import AgentClient
from session_controller import SessionController, SessionState
from config import *

# 全局双工模式标志,启动时检测设置
is_duplex = False

# ============ 设备自动检测 ============
def _init_audio_devices():
    """ 自动检测音频设备,配置中为 None 的项自动填充"""
    global A2DP_ID, A2DP_SR, HFP_IN, HFP_IN_SR
    if A2DP_ID is not None and HFP_IN is not None:
        print(f"[Audio] 使用配置: 输入=#{HFP_IN}({HFP_IN_SR}Hz) 输出=#{A2DP_ID}({A2DP_SR}Hz)")
        return
    prefer = getattr(__import__('config'), 'PREFER_LOCAL', False)
    det = auto_detect_devices(prefer_local=prefer)
    _init_audio_devices._det = det  # 保存检测结果供双工检测用
    if HFP_IN is None:
        HFP_IN = det['input_id']
        HFP_IN_SR = det['input_sr']
    if A2DP_ID is None:
        A2DP_ID = det['output_id']
        A2DP_SR = det['output_sr']

_init_audio_devices()

# ============ 设备双工检测 ============
def _check_duplex():
    """ 检测设备是否支持同时输入输出,设置全局 is_duplex 标志 """
    global is_duplex
    if DUPLEX_MODE is not None:
        is_duplex = DUPLEX_MODE
        mode_str = "全双工(边说边听)" if is_duplex else "半双工(交替模式)"
        print(f"[Audio] 双工模式(配置指定): {mode_str}")
        return

    print("[Audio] 正在检测设备双工能力...")
    # 蓝牙一体设备 (HFP输入+A2DP输出) 在 Windows 下不支持全双工
    if hasattr(_init_audio_devices, '_det') and _init_audio_devices._det.get('mode') == 'bt_unified':
        is_duplex = False
        print(f"[Audio] 检测结果: 半双工(交替模式) - 蓝牙一体设备 HFP/A2DP 不可同时工作")
        return
    result = check_duplex_support(HFP_IN, HFP_IN_SR, A2DP_ID, A2DP_SR, test_duration=1.0)
    is_duplex = result["duplex"]
    mode_str = "全双工(边说边听)" if is_duplex else "半双工(交替模式)"
    print(f"[Audio] 检测结果: {mode_str} - {result['reason']}")

_check_duplex()

SYSTEM_PROMPT = f"""你是"小龙",一个运行在用户电脑上的语音助手。你通过耳机与用户进行实时语音对话。

★★★ 交互方式说明 ★★★
- 用户通过麦克风说话,语音经 ASR(语音识别)转成文字后发送给你
- 你的回复会经过 TTS(语音合成)播报给用户
- 所以你收到的是语音转写文本,可能有少量识别错误,请根据上下文理解用户意图
- 你是一个语音助手,"听"和"说"都是通过这套语音管线完成的,不要说自己听不到或看不到

★★★ 最重要的规则 ★★★
你的回复会被 TTS 实时播报,用户在等你说话。
所以你必须:先说一句话(如"好的,我来查一下"),然后再执行工具/命令。
绝对禁止先执行工具再说话,否则用户会长时间听不到任何声音。

核心能力：
1. 完整的系统操作能力：执行任意命令行、读写文件、安装软件、管理进程
2. 联网能力：用 curl/wget 搜索、下载、访问 API、爬取网页
3. 编程能力：Python/Node/PowerShell等
4. 遇到不会的事,主动搜索解决方案

回复规则(回复会被 TTS 播放)：
1. 简洁口语化,禁止 markdown、表格、代码块、emoji、特殊符号
2. 一般回复 2-3 句话,列举不超过3条
3. 执行完只说结果,不重复过程

行动原则：
- 用户让你做什么就做什么,不要反问"你确定吗"
- 缺少工具就安装,缺少文件就下载,主动解决问题
- 查询信息可以用 curl 访问搜索引擎或 API
- 充分发挥你的编程和系统操作能力,做一个真正有用的助手

★★★ 连续对话模式 ★★★
你可以开启"连续对话模式",开启后用户不需要说"小龙"前缀,直接说话就能与你交流。

触发条件(符合任一即可)：
1. 用户主动要求连续对话,如"我们聊聊天""和我对话练习"
2. 场景需要多轮交互,如：英语口语练习、闲聊、问题探讨、故事接龙、角色扮演、面试模拟、语言学习等

开启方式：
在回复中包含"开始连续对话"这几个字。例如："好的,我们开始连续对话,你先来一句吧"

结束方式：
在回复中包含"结束连续对话"这几个字。例如："好的,结束连续对话,有需要再叫我"
结束时机：用户主动说"结束""不聊了""停止对话"等,或你判断对话自然结束时

注意：连续对话模式下回复要更简短自然,像真人聊天一样。"""


def clean_for_speech(text):
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'[#*`\[\]()]', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[\U0001F600-\U0001F9FF]', '', text)
    text = re.sub(r'-\s+', '', text)  # 去列表标记
    return text.strip()


def resample_to_a2dp(audio_float32):
    """24kHz float32 → A2DP采样率 float32"""
    return fast_resample(audio_float32, 24000, A2DP_SR)


def play_audio(audio_float32, first=False):
    out = resample_to_a2dp(audio_float32)
    if first:
        out = np.concatenate([np.zeros(int(A2DP_SR * 0.25), dtype=np.float32), out])
    sd.play(out, samplerate=A2DP_SR, device=A2DP_ID)
    sd.wait()


# ============ 全局组件 ============
asr = ASREngine()
tts = TTSEngine(voice=TTS_VOICE, rate=TTS_RATE)
agent = AgentClient(working_dir=PI_WORKING_DIR, provider=PI_PROVIDER, model=PI_MODEL)
session = SessionController()
recorder = AudioRecorder(device_id=HFP_IN, sample_rate=HFP_IN_SR, target_sr=16000,
                         block_size=HFP_IN_SR // 10)

running = True
processing = False
long_input_mode = False      # 长输入模式
input_buffer = []            # 输入积累缓冲
input_timer = None           # 静音超时定时器


def feed_audio(data):
    asr.feed_audio(data)


def play_simple(text):
    if not is_duplex:
        recorder.stop()
    sd.stop()  # 确保之前的播放已停止
    time.sleep(0.15)
    print(f"[TTS] {text}", flush=True)
    audio = tts.synthesize(text)
    if audio is not None:
        print(f"  [TTS] 合成OK: {len(audio)} samples", flush=True)
        play_audio(audio, first=True)
        print(f"  [TTS] 播放完成", flush=True)
    else:
        print(f"  [TTS] 合成失败!", flush=True)
    time.sleep(0.1)
    asr.reset()
    if not is_duplex:
        recorder.start(callback=feed_audio)


def speak_async(text, then_state=None):
    def _w():
        play_simple(text)
        if then_state:
            session.set_state(then_state)
    threading.Thread(target=_w, daemon=True).start()


# ============ 打断监听 ============
_STOP_KEYWORDS = ['终止', '中止', '钟止', '中指', '种植']

def start_interrupt_listen(stop_event):
    """开始监听打断(非阻塞),检测到'终止'时设置stop_event"""
    def _on_final(text):
        print(f"\n  [监听] {text}", flush=True)
        if any(kw in text for kw in _STOP_KEYWORDS) or 'stop' in text.lower():
            stop_event.set()

    asr.set_callbacks(on_final=_on_final)
    if is_duplex:
        # 全双工模式:录音一直在跑,只需重置ASR
        asr.reset()
    else:
        # 半双工模式:需要重新启动录音
        asr.reset()
        recorder.start(callback=feed_audio)

def stop_interrupt_listen():
    """停止监听"""
    if not is_duplex:
        recorder.stop()
    time.sleep(0.05)


# ============ Agent 指令处理 ============
def handle_command(cmd):
    global processing
    processing = True
    print(f"\n{'='*50}", flush=True)
    print(f"[用户] {cmd}", flush=True)
    print(f"{'='*50}", flush=True)

    if is_duplex:
        # 全双工模式:录音不停,直接设置打断监听
        asr.reset()
    else:
        # 半双工模式:停止录音
        recorder.stop()
    time.sleep(0.1)

    # 流式文本收集
    sentence_queue = queue.Queue()
    buf = {"text": "", "done": False}
    # 切句:逗号切短句,句号/问号/叹号切长句
    CLAUSE_PAT = re.compile(r'[,,;;、]|[。!?!?\n]')

    def on_delta(delta):
        print(delta, end="", flush=True)
        buf["text"] += delta
        while True:
            m = CLAUSE_PAT.search(buf["text"])
            if not m:
                break
            pos = m.end()
            sep_char = m.group()
            is_sentence_end = sep_char in '。!?!?\n'
            s = clean_for_speech(buf["text"][:pos].strip())
            buf["text"] = buf["text"][pos:]
            if s and len(s) > 1:
                sentence_queue.put((s, is_sentence_end))

    def on_complete(full):
        print(flush=True)
        r = clean_for_speech(buf["text"].strip())
        if r and len(r) > 1:
            sentence_queue.put((r, True))
        buf["text"] = ""
        buf["done"] = True
        # 打印完整的 agent 回复日志
        full_text = full.strip() if full else ""
        print(f"\n{'-'*50}", flush=True)
        print(f"[小龙] {full_text}", flush=True)
        print(f"{'-'*50}", flush=True)
        # 检测 agent 输出中的连续对话标记
        if SessionController.check_continuous_end(full_text):
            session.exit_continuous_mode("代理结束")
        elif SessionController.check_continuous_start(full_text):
            session.enter_continuous_mode()
        # 连续对话活动时间在播报完成后刷新(见 handle_command 末尾)

    agent.set_callbacks(on_text_delta=on_delta, on_response_complete=on_complete)
    agent.prompt_async(cmd)

    # ========== 并发合成 + FIFO播放 ==========
    #
    # clauses[i] = {text, is_sent_end, audio, ready(Event)}
    #   - 每个短句独立合成
    #
    # merges[(start, end)] = {text, audio, ready(Event)}
    #   - 合并文本的 TTS 结果,让 TTS 理解上下文产生连贯语气
    #   - 最多合4个短句,不跨句号边界
    #
    # 播放时:从 play_idx 找最长已就绪的合并音频,没有就用单句
    #
    clauses = []          # 有序短句列表
    merges = {}           # {(start, end): {text, audio, ready}}
    clauses_lock = threading.Lock()
    all_text_done = threading.Event()
    aborted = False
    stop_event = threading.Event()
    listening = False
    first_play = True
    synth_sem = threading.Semaphore(4)  # 最多4个并发合成

    # 全双工模式:录音一直在跑,直接启动打断监听
    if is_duplex:
        start_interrupt_listen(stop_event)
        listening = True

    def _do_synth(item):
        """合成一个音频项(clause 或 merge)"""
        synth_sem.acquire()
        try:
            if stop_event.is_set():
                return
            item["audio"] = tts.synthesize(item["text"])
        finally:
            item["ready"].set()
            synth_sem.release()

    def _submit_merges_for(new_idx):
        """新短句到达后,创建以它结尾的合并项(长度2~MAX_MERGE_CLAUSES)"""
        with clauses_lock:
            for length in range(2, MAX_MERGE_CLAUSES + 1):
                start = new_idx - length + 1
                if start < 0:
                    continue
                # 检查中间不跨句号边界:start ~ new_idx-1 都不能是句末
                can_merge = True
                for i in range(start, new_idx):
                    if clauses[i]["is_sent_end"]:
                        can_merge = False
                        break
                if not can_merge:
                    continue
                key = (start, new_idx)
                if key in merges:
                    continue
                # 拼接合并文本(用逗号连接)
                merged_text = ",".join(clauses[i]["text"] for i in range(start, new_idx + 1))
                merge_item = {"text": merged_text, "audio": None, "ready": threading.Event()}
                merges[key] = merge_item

            # 复制要合成的项(锁外启动线程)
            new_merges = {k: v for k, v in merges.items()
                         if k[1] == new_idx and not v["ready"].is_set()}

        for key, item in new_merges.items():
            print(f"  [合成{key}] {item['text'][:50]}", flush=True)
            threading.Thread(target=_do_synth, args=(item,), daemon=True).start()

    def _collector():
        """收集线程:取短句 → 启动单句合成 + 合并合成"""
        while not stop_event.is_set():
            try:
                text, is_sent_end = sentence_queue.get(timeout=0.3)
            except queue.Empty:
                if buf["done"] and sentence_queue.empty():
                    break
                continue

            item = {"text": text, "is_sent_end": is_sent_end,
                    "audio": None, "ready": threading.Event()}
            with clauses_lock:
                idx = len(clauses)
                clauses.append(item)

            print(f"  [#{idx}] {text[:40]}{'。' if is_sent_end else ','}", flush=True)
            threading.Thread(target=_do_synth, args=(item,), daemon=True).start()
            _submit_merges_for(idx)

        all_text_done.set()

    collector_thread = threading.Thread(target=_collector, daemon=True)
    collector_thread.start()

    # ========== 主线程:FIFO顺序播放 ==========
    play_idx = 0
    first_play = True  # 首次播放需要BT切换

    def _find_best_audio():
        """从 play_idx 开始,找最长的已就绪合并音频。
        返回 (audio, text, next_play_idx) 或 None"""
        with clauses_lock:
            n = len(clauses)
        if play_idx >= n:
            return None
        for length in range(min(MAX_MERGE_CLAUSES, n - play_idx), 1, -1):
            end = play_idx + length - 1
            key = (play_idx, end)
            if key in merges:
                m = merges[key]
                if m["ready"].is_set() and m["audio"] is not None:
                    return (m["audio"], m["text"], end + 1)
        with clauses_lock:
            c = clauses[play_idx]
        if c["ready"].is_set() and c["audio"] is not None:
            return (c["audio"], c["text"], play_idx + 1)
        return None

    while True:
        if stop_event.is_set():
            print("\n[打断] 用户说终止", flush=True)
            if listening:
                stop_interrupt_listen()
                listening = False
            agent.abort()
            aborted = True
            agent._response_event.wait(timeout=5)
            while not sentence_queue.empty():
                try: sentence_queue.get_nowait()
                except: break
            time.sleep(1)
            play_simple("好的,已终止")
            break

        with clauses_lock:
            has_more = play_idx < len(clauses)

        if not has_more:
            if all_text_done.is_set():
                break
            if not listening and not is_duplex:
                start_interrupt_listen(stop_event)
                listening = True
            time.sleep(0.2)
            continue

        # 找最佳音频
        best = _find_best_audio()
        if best is None:
            with clauses_lock:
                c = clauses[play_idx]
            if not c["ready"].wait(timeout=0.3):
                continue
            best = _find_best_audio()
            if best is None:
                play_idx += 1
                continue

        audio, text, next_idx = best
        span = next_idx - play_idx

        # 单句就绪但合并项正在合成 → 短等给合并机会
        # 首2句不等合并,直接播放,减少首次响应延迟
        if span == 1 and play_idx >= 2:
            pending_keys = [k for k in merges if k[0] == play_idx and not merges[k]["ready"].is_set()]
            if pending_keys:
                for mk in pending_keys:
                    merges[mk]["ready"].wait(timeout=0.3)
                better = _find_best_audio()
                if better is not None:
                    audio, text, next_idx = better
                    span = next_idx - play_idx

        # HFP→A2DP 切换(仅在半双工监听后需要)
        if listening and not is_duplex:
            stop_interrupt_listen()
            listening = False
            sd.stop()
            time.sleep(0.25)
            first_play = True  # 切换后需要重新加静音前缀

        if stop_event.is_set():
            continue

        # 首次播放:加静音前缀让蓝牙就绪
        if first_play:
            sd.stop()
            time.sleep(0.15)

        tag = f"x{span}" if span > 1 else ""
        print(f"  [播放{tag}] {text[:60]}", flush=True)
        play_audio(audio, first=first_play)
        first_play = False
        play_idx = next_idx

    collector_thread.join(timeout=5)

    if not aborted:
        agent._response_event.wait(timeout=10)

    if listening:
        stop_interrupt_listen()

    processing = False
    session.set_state(SessionState.ACTIVE)
    # TTS 播报全部完成后再刷新连续对话活动时间
    if session.continuous_mode:
        session.refresh_continuous_activity()
    # 恢复正常 ASR 回调(打断监听期间会被替换)
    asr.set_callbacks(on_final=on_asr_final)
    asr.reset()
    if not is_duplex:
        recorder.start(callback=feed_audio)

    # 检查是否有排队的指令
    queued = session.pop_queued_command()
    if queued:
        print(f"\n[排队指令] 执行: {queued}", flush=True)
        on_command(queued)
        return

    print(flush=True)


# ============ 输入积累逻辑 ============
def flush_input_buffer():
    """把积累的输入合并发送给agent"""
    global input_buffer, input_timer, long_input_mode
    if not input_buffer:
        return
    full_cmd = "。".join(input_buffer)
    input_buffer = []
    long_input_mode = False
    input_timer = None
    print(f"\n[合并输入] {full_cmd}", flush=True)
    session.set_state(SessionState.PROCESSING)
    threading.Thread(target=handle_command, args=(full_cmd,), daemon=True).start()


def reset_input_timer():
    """重置静音超时定时器"""
    global input_timer
    if input_timer:
        input_timer.cancel()
    input_timer = threading.Timer(INPUT_SILENCE_TIMEOUT, flush_input_buffer)
    input_timer.start()


# ============ 会话回调 ============
def on_wake():
    speak_async("我在,请说")

def on_sleep():
    global long_input_mode, input_buffer
    long_input_mode = False
    input_buffer = []
    speak_async("好的,再见")

def on_command(cmd):
    global long_input_mode, input_buffer

    if processing:
        # 播放中无法用此路径打断(打断由 listen_for_interrupt 处理)
        return

    # 检测长输入模式触发
    if re.search(r'(长段|长篇|多段|详细)(输入|说明|描述)', cmd):
        long_input_mode = True
        input_buffer = []
        speak_async("好的,请说,说完后说好了")
        return

    if long_input_mode:
        # 长输入模式:检测"好了"结束
        if re.search(r'^好了[。..!!]?$', cmd.strip()):
            flush_input_buffer()
        else:
            input_buffer.append(cmd)
            print(f"  [积累] {cmd} (共{len(input_buffer)}段)", flush=True)
        return

    # 普通模式:检测"好了"作为结束标记,或等静音超时
    if re.search(r'^好了[。..!!]?$', cmd.strip()):
        if input_buffer:
            flush_input_buffer()
        return
    input_buffer.append(cmd)
    print(f"  [积累] {cmd} (说'好了'或等{INPUT_SILENCE_TIMEOUT}s静音)", flush=True)
    reset_input_timer()

    # 保持 ACTIVE 状态以继续接收后续输入(上下文拼接)
    # session_controller 在调用 on_command 前已转为 PROCESSING,
    # 这里重置为 ACTIVE + pending,让后续语音识别结果能继续累积
    # 注意: on_command 是在 session._lock 持有期间被回调的,
    # 所以这里直接设置属性,不能再加锁(否则死锁)
    session.state = SessionState.ACTIVE
    session._pending_command = True
    session._pending_time = time.time()

def on_continuous_start():
    print("[Main] 🔄 连续对话模式开启,用户可直接说话", flush=True)

def on_continuous_end():
    print("[Main] ■ 连续对话模式结束,恢复唤醒词模式", flush=True)
    speak_async("连续对话已结束,需要时再叫我")

session.set_callbacks(on_wake=on_wake, on_sleep=on_sleep, on_command=on_command,
                      on_continuous_start=on_continuous_start,
                      on_continuous_end=on_continuous_end)


# ============ ASR 回调 ============
def on_asr_final(text):
    print(f"\n  [识别] {text}", flush=True)
    session.process_text(text, is_final=True)

asr.set_callbacks(on_final=on_asr_final)


# ============ 主函数 ============
def main():
    global running

    duplex_str = "全双工(边说边听)" if is_duplex else "半双工(交替模式)"
    print("=" * 50)
    print("  🎧 小龙语音助手")
    print(f"  音频模式: {duplex_str}")
    print("  '小龙小龙' 唤醒 | '小龙小龙退下' 休眠")
    print("  '小龙,xxx' 发送指令(等静音后发送)")
    print("  '小龙,长段输入' → 说完后说'好了'")
    print("  播放中说 '终止' 打断")
    print("  连续对话: agent自动开启/关闭")
    print("  Ctrl+C 退出")
    print("=" * 50, flush=True)

    print("\n[Init] ASR...", flush=True)
    asr.init()
    print("[Init] TTS 预缓存...", flush=True)
    tts.precache()
    print("[Init] OpenClaw Agent...", flush=True)
    agent.start()
    print("[Init] 系统提示词...", flush=True)
    # Gateway 模式:system prompt 通过 workspace 文件配置
    # steer 命令通过 bridge 传递(bridge 会忽略但记录日志)
    agent._send({"type": "steer", "message": SYSTEM_PROMPT})
    agent.save_steer(SYSTEM_PROMPT)
    time.sleep(0.5)
    print("[Init] ✅ 就绪\n", flush=True)

    recorder.start(callback=feed_audio)
    play_simple("语音助手已启动,说小龙小龙唤醒我")

    def auto_sleep():
        while running:
            session.check_auto_sleep()
            session.check_continuous_timeout()
            time.sleep(3)
    threading.Thread(target=auto_sleep, daemon=True).start()

    print("\n等待语音输入...\n", flush=True)
    try:
        while running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    recorder.stop()
    asr.stop()
    agent.stop()
    print("语音助手已关闭")


if __name__ == "__main__":
    main()
