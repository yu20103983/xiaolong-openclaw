"""会话控制器 - 状态机 + 唤醒词检测 + 指令分发

支持:
  - 唤醒词 "小龙小龙",只要1个"小龙"(含近音)即唤醒
  - 指令 "小龙,xxx",宽松匹配前缀
  - 上下文关联:用户说"小龙"(停顿)"帮我xxx" → 自动拼接为指令
  - 休眠 "小龙(小龙)退下/再见",宽松匹配
  - 处理中指令排队:agent处理中用户说新指令自动缓存
"""

import re
import time
import threading
from enum import Enum
from typing import Optional, Callable
from config import AUTO_SLEEP_TIMEOUT, CONTINUOUS_SILENCE_TIMEOUT


class SessionState(Enum):
    SLEEPING = "sleeping"   # 休眠:只监听唤醒词
    ACTIVE = "active"       # 活跃:监听指令
    PROCESSING = "processing"  # 处理中:等待 Pi 响应
    SPEAKING = "speaking"   # 播报中:TTS 输出


# 连续对话模式标记词
_CONTINUOUS_START_PATTERNS = [
    r'开始连续对话',
    r'进入连续对话',
    r'连续对话模式',
]
_CONTINUOUS_END_PATTERNS = [
    r'结束连续对话',
    r'退出连续对话',
    r'连续对话结束',
]


# ============ 模糊匹配工具 ============

# "小龙" 的常见 ASR 误识别变体
_XIAO_CHARS = r"[小肖晓消笑筱享向想响销削校效啸歇]"
_LONG_CHARS = r"[龙隆笼聋拢弄农浓侬绒容融荣龍东]"
# 匹配一个 "小龙" (含变体),中间允许0-1个杂字
_ONE_XL = _XIAO_CHARS + r".{0,1}" + _LONG_CHARS
# 匹配 "小龙小龙" (两次),中间允许杂字/标点
_TWO_XL = _ONE_XL + r".{0,3}" + _ONE_XL
# 匹配 "X龙X龙" 模式(任意字+龙 重复两次)
_ANY_LONG_TWICE = r"." + _LONG_CHARS + r".{0,3}." + _LONG_CHARS

# "退下" 的常见变体
_TUI_CHARS = r"[退对腿推堆吹]"
_XIA_CHARS = r"[下夏吓侠]"
_TUIXIA = _TUI_CHARS + r".{0,1}" + _XIA_CHARS

# "再见" 的变体
_ZAIJIAN = r"再.{0,1}见"

# "不说了" "不聊了" "我走了" 等其他休眠表达
_SLEEP_EXTRAS = [
    r'不说了', r'不聊了', r'不用了', r'我走了',
    r'先这样', r'就这样', r'没事了',
    r'去休息', r'去睡觉', r'下次再说',
    r'拜拜', r'打扰了',
]

# 等待后续指令的超时秒数(用户说"小龙"后的等待窗口)
_PENDING_TIMEOUT = 5.0


def _has_wake_word(text: str) -> bool:
    """检测文本中是否包含至少一个 '小龙' (含近音变体)
    或 'X乐X乐' 模式(任意字+乐 重复两次)"""
    if re.search(_ONE_XL, text):
        return True
    if re.search(_ANY_LONG_TWICE, text):
        return True
    return False


def _is_only_wake_word(text: str) -> bool:
    """检测文本是否只包含'小龙'(1~2次),没有其他有意义的内容"""
    cleaned = re.sub(_ONE_XL, '', text)
    cleaned = re.sub(r'[,,::。.、\s!!??]', '', cleaned)
    return len(cleaned) == 0


def _strip_wake_prefix(text: str) -> str:
    """去掉文本开头的 '小龙' 前缀(含标点分隔),返回指令部分"""
    # 先尝试去掉 "小龙小龙" 前缀
    m = re.match(_TWO_XL + r"[,,::。.、\s]*", text)
    if m:
        return text[m.end():].strip()
    # 再尝试去掉单个 "小龙" 前缀
    m = re.match(_ONE_XL + r"[,,::。.、\s]*", text)
    if m:
        return text[m.end():].strip()
    # 尝试去掉 "X乐X乐" 前缀
    m = re.match(_ANY_LONG_TWICE + r"[,,::。.、\s]*", text)
    if m:
        return text[m.end():].strip()
    # 尝试去掉开头的单独 "乐" (被截断的前缀)
    m = re.match(_LONG_CHARS + r"[,,::。.、\s]*", text)
    if m:
        return text[m.end():].strip()
    return ""


def _is_sleep_command(text: str) -> bool:
    """检测是否是休眠指令: 小龙(小龙)退下/再见/不聊了..."""
    if not _has_wake_word(text):
        return False
    if re.search(_TUIXIA, text):
        return True
    if re.search(_ZAIJIAN, text):
        return True
    for pat in _SLEEP_EXTRAS:
        if re.search(pat, text):
            return True
    return False


# 用于 _extract_after_long 的严格字符集（只匹配真正接近"龙"的字，避免误触发）
_LONG_STRICT = r"[龙隆笼聋拢龍]"


def _extract_after_long(text: str) -> Optional[str]:
    """从文本中任意位置找"龙"(严格近音),提取其后的内容作为指令。
    例如: '什么龙帮我查天气' → '帮我查天气'
          '龙,查一下' → '查一下'
    """
    m = re.search(_LONG_STRICT + r'[,，::。.、\s]*', text)
    if not m:
        return None
    cmd = text[m.end():].strip()
    cmd = re.sub(r'[。．.\uff01!？?]+$', '', cmd).strip()
    if not cmd or len(cmd) <= 1:
        return None
    # 确保不是又一个"龙"
    if _is_only_wake_word(cmd):
        return None
    return cmd


# ============ 会话控制器 ============

class SessionController:
    """会话状态机控制器"""

    def __init__(self):
        self.state = SessionState.SLEEPING
        self._on_wake: Optional[Callable[[], None]] = None
        self._on_sleep: Optional[Callable[[], None]] = None
        self._on_command: Optional[Callable[[str], None]] = None
        self._on_continuous_start: Optional[Callable[[], None]] = None
        self._on_continuous_end: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()
        self._last_activity = time.time()
        self._auto_sleep_timeout = AUTO_SLEEP_TIMEOUT
        # 上下文关联:用户说了"小龙"但没跟指令,等待下一句
        self._pending_command = False
        self._pending_time = 0.0
        # 排队指令队列: agent处理中时用户说的新指令(可多条拼接)
        self._queued_commands: list = []
        # 连续对话模式
        self.continuous_mode = False
        self._continuous_last_activity = 0.0
        self._continuous_silence_timeout = CONTINUOUS_SILENCE_TIMEOUT

    def set_callbacks(self,
                      on_wake: Optional[Callable[[], None]] = None,
                      on_sleep: Optional[Callable[[], None]] = None,
                      on_command: Optional[Callable[[str], None]] = None,
                      on_continuous_start: Optional[Callable[[], None]] = None,
                      on_continuous_end: Optional[Callable[[], None]] = None):
        self._on_wake = on_wake
        self._on_sleep = on_sleep
        self._on_command = on_command
        self._on_continuous_start = on_continuous_start
        self._on_continuous_end = on_continuous_end

    def process_text(self, text: str, is_final: bool = False):
        """处理 ASR 识别出的文本"""
        if not text:
            return

        text = text.strip()

        with self._lock:
            if self.state == SessionState.SLEEPING:
                self._handle_sleeping(text)
            elif self.state == SessionState.ACTIVE:
                if is_final:
                    self._handle_active(text)
            elif self.state == SessionState.PROCESSING:
                if is_final:
                    self._handle_processing(text)
            # SPEAKING 状态下忽略输入

    def _handle_sleeping(self, text: str):
        """休眠状态:检测唤醒词
        只要出现至少一个 '小龙' 即唤醒
        """
        if _has_wake_word(text):
            self._transition(SessionState.ACTIVE)
            self._pending_command = False
            print(f"[Session] 唤醒! ({text})")

            # 唤醒的同时检查是否带了指令("小龙帮我xxx")
            cmd = self._try_extract_command(text)
            if cmd:
                # 唤醒 + 指令一起来了,不播唤醒提示,直接执行
                print(f"[Session] 唤醒即指令: {cmd}")
                self._transition(SessionState.PROCESSING)
                if self._on_command:
                    self._on_command(cmd)
            else:
                # 只是唤醒,等后续指令
                self._pending_command = True
                self._pending_time = time.time()
                if self._on_wake:
                    self._on_wake()

    def _handle_active(self, text: str):
        """活跃状态:检测休眠词或提取指令
        支持上下文关联:
          - "小龙"(停顿)"帮我放歌" → 第一句设置pending,第二句作为指令
          - "小龙,帮我放歌" → 直接提取指令
        连续对话模式下,所有语音直接作为指令(依然支持休眠词)
        """
        self._last_activity = time.time()

        # 1. 检查休眠词(连续对话模式下也生效)
        if _is_sleep_command(text):
            self._pending_command = False
            if self.continuous_mode:
                self.continuous_mode = False
                print(f"[Session] 连续对话模式结束 (休眠)")
            self._transition(SessionState.SLEEPING)
            print(f"[Session] 休眠 ({text})")
            if self._on_sleep:
                self._on_sleep()
            return

        # 2. 尝试从文本中提取带"小龙"前缀的指令
        command = self._try_extract_command(text)
        if command:
            self._pending_command = False
            self._transition(SessionState.PROCESSING)
            print(f"[Session] 指令: {command}")
            if self._on_command:
                self._on_command(command)
            return

        # 3. 文本包含"小龙"但没有提取到指令(只喊了名字)
        if _has_wake_word(text) and _is_only_wake_word(text):
            self._pending_command = True
            self._pending_time = time.time()
            print(f"[Session] 等待指令... ({text})")
            return

        # 4. 文本中任意位置含"龙"→ 提取龙后内容作为指令
        long_cmd = _extract_after_long(text)
        if long_cmd:
            self._pending_command = False
            self._transition(SessionState.PROCESSING)
            print(f"[Session] 指令(龙后): {long_cmd}")
            if self._on_command:
                self._on_command(long_cmd)
            return
        # 只有"龙"没有后续内容 → 视为截断的唤醒词
        if re.search(_LONG_STRICT, text):
            cleaned = re.sub(_LONG_STRICT, '', text)
            cleaned = re.sub(r'[,,::。.、\s!!??]', '', cleaned)
            if len(cleaned) == 0:
                self._pending_command = True
                self._pending_time = time.time()
                print(f"[Session] 等待指令(龙前缀)... ({text})")
                return

        # 5. 上下文关联:前面刚说了"小龙",这句是指令内容
        if self._pending_command:
            elapsed = time.time() - self._pending_time
            if elapsed <= _PENDING_TIMEOUT:
                # 去掉可能的前缀标点
                cmd = re.sub(r'^[,,::。.、\s]+', '', text)
                cmd = re.sub(r'[。..!!??]+$', '', cmd).strip()
                if cmd and len(cmd) > 1:
                    self._pending_command = False
                    self._transition(SessionState.PROCESSING)
                    print(f"[Session] 关联指令: {cmd} (间隔{elapsed:.1f}s)")
                    if self._on_command:
                        self._on_command(cmd)
                    return
            else:
                # 超时,清除pending
                self._pending_command = False
                print(f"[Session] 等待超时,忽略 ({text})")

        # 5b. 连续对话模式:任何语音直接作为指令
        if self.continuous_mode:
            cmd = re.sub(r'^[,,::。.、\s]+', '', text)
            cmd = re.sub(r'[。..!!??]+$', '', cmd).strip()
            if cmd and len(cmd) > 1:
                self._continuous_last_activity = time.time()
                self._pending_command = False
                self._transition(SessionState.PROCESSING)
                print(f"[Session] 连续对话指令: {cmd}")
                if self._on_command:
                    self._on_command(cmd)
                return

        # 6. 不含"小龙"且非pending → 忽略(环境噪音)

    def _handle_processing(self, text: str):
        """处理中状态:缓存用户新指令,等agent完成后执行"""
        # 检测休眠词
        if _is_sleep_command(text):
            self._queued_commands.clear()
            self._transition(SessionState.SLEEPING)
            print(f"[Session] 处理中收到休眠指令 ({text})")
            if self._on_sleep:
                self._on_sleep()
            return

        # 尝试提取指令
        command = self._try_extract_command(text)
        if command:
            self._queued_commands.append(command)
            print(f"[Session] ▇ 排队指令({len(self._queued_commands)}): {command}")
            return

        # 任意位置含"龙"→ 提取龙后内容作为指令排队
        long_cmd = _extract_after_long(text)
        if long_cmd:
            self._queued_commands.append(long_cmd)
            print(f"[Session] ▇ 排队指令({len(self._queued_commands)},龙后): {long_cmd}")
            return

        # 其他文本忽略(环境噪音/agent回复被麦克风捕获)

    def pop_queued_command(self) -> Optional[str]:
        """取出并清除所有排队指令,拼接返回"""
        with self._lock:
            if not self._queued_commands:
                return None
            combined = "。".join(self._queued_commands)
            self._queued_commands.clear()
            return combined

    @property
    def has_queued_command(self) -> bool:
        """是否有排队指令(无锁快速检查)"""
        return len(self._queued_commands) > 0

    def queue_command(self, cmd: str):
        """直接添加指令到排队队列(不需要唤醒词)"""
        with self._lock:
            self._queued_commands.append(cmd)
            print(f"[Session] ■ 排队指令({len(self._queued_commands)}): {cmd}")

    def _try_extract_command(self, text: str) -> Optional[str]:
        """从文本中提取指令内容
        策略:
          1. 文本包含"小龙" → 去掉前缀,剩余即指令
          2. 指令部分不能为空或太短(<=1字)
          3. 如果剩余部分又是"小龙"本身,返回None
        """
        if not _has_wake_word(text):
            return None

        cmd = _strip_wake_prefix(text)

        # 去掉开头和末尾的标点符号
        cmd = re.sub(r'^[,,::。.、\s]+', '', cmd)
        cmd = re.sub(r'[。..!!??]+$', '', cmd).strip()

        if not cmd or len(cmd) <= 1:
            return None

        # 如果剩余部分又是 "小龙" 本身,不当指令
        if _is_only_wake_word(cmd):
            return None

        return cmd

    def _transition(self, new_state: SessionState):
        """状态转换"""
        old = self.state
        self.state = new_state
        print(f"[Session] {old.value} -> {new_state.value}")

    def set_state(self, state: SessionState):
        """直接设置状态"""
        with self._lock:
            self._transition(state)

    def enter_continuous_mode(self):
        """进入连续对话模式"""
        with self._lock:
            if self.continuous_mode:
                return
            self.continuous_mode = True
            self._continuous_last_activity = time.time()
            print("[Session] → 连续对话模式开启")
            if self._on_continuous_start:
                self._on_continuous_start()

    def exit_continuous_mode(self, reason: str = ""):
        """退出连续对话模式"""
        with self._lock:
            if not self.continuous_mode:
                return
            self.continuous_mode = False
            print(f"[Session] ← 连续对话模式结束 ({reason})")
            if self._on_continuous_end:
                self._on_continuous_end()

    def refresh_continuous_activity(self):
        """刷新连续对话模式的活动时间(agent回复完成时调用)"""
        self._continuous_last_activity = time.time()

    def check_continuous_timeout(self):
        """检查连续对话模式是否超时"""
        if not self.continuous_mode:
            return
        if self._continuous_silence_timeout <= 0:
            return
        if self.state != SessionState.ACTIVE:
            return  # 处理中/播报中不计算超时
        elapsed = time.time() - self._continuous_last_activity
        if elapsed > self._continuous_silence_timeout:
            self.exit_continuous_mode("用户沉默超时")

    @staticmethod
    def check_continuous_start(text: str) -> bool:
        """检测 agent 输出是否包含连续对话开始标记"""
        for pat in _CONTINUOUS_START_PATTERNS:
            if re.search(pat, text):
                return True
        return False

    @staticmethod
    def check_continuous_end(text: str) -> bool:
        """检测 agent 输出是否包含连续对话结束标记"""
        for pat in _CONTINUOUS_END_PATTERNS:
            if re.search(pat, text):
                return True
        return False

    def check_auto_sleep(self):
        """检查是否需要自动休眠"""
        if self._auto_sleep_timeout <= 0:
            return
        if (self.state == SessionState.ACTIVE and
                time.time() - self._last_activity > self._auto_sleep_timeout):
            self.set_state(SessionState.SLEEPING)
            print("[Session] 自动休眠(超时)")
            if self._on_sleep:
                self._on_sleep()


# ============ 测试 ============
if __name__ == "__main__":
    import sys

    print("=== 模糊匹配单元测试 ===\n")

    # 唤醒测试
    wake_tests = [
        ("小龙小龙", True),
        ("小龙你好", True),
        ("肖龙", True),
        ("晓隆晓隆", True),
        ("小拢小拢", True),
        ("小农", True),
        ("笑龙你好", True),
        ("享龙享龙", True),   # X龙X龙 模式
        ("享龙", True),         # 享 在 _XIAO_CHARS 中
        ("今天天气不错", False),
        ("好的", False),
    ]
    print("--- 唤醒词检测 ---")
    for text, expected in wake_tests:
        result = _has_wake_word(text)
        status = "OK" if result == expected else "FAIL"
        print(f"  [{status}] '{text}' -> {result} (期望{expected})")

    # 纯唤醒词检测
    only_tests = [
        ("小龙", True),
        ("小龙小龙", True),
        ("小龙。", True),
        ("小龙,帮我", False),
        ("小龙帮我查天气", False),
    ]
    print("\n--- 纯唤醒词(无指令)检测 ---")
    for text, expected in only_tests:
        result = _has_wake_word(text) and _is_only_wake_word(text)
        status = "OK" if result == expected else "FAIL"
        print(f"  [{status}] '{text}' -> {result} (期望{expected})")

    # 休眠测试
    sleep_tests = [
        ("小龙小龙退下", True),
        ("小龙退下", True),
        ("小隆对下", True),
        ("小龙再见", True),
        ("晓龙推下", True),
        ("退下", False),
        ("再见", False),
    ]
    print("\n--- 休眠词检测 ---")
    for text, expected in sleep_tests:
        result = _is_sleep_command(text)
        status = "OK" if result == expected else "FAIL"
        print(f"  [{status}] '{text}' -> {result} (期望{expected})")

    # 指令提取测试
    cmd_tests = [
        ("小龙,帮我查天气", "帮我查天气"),
        ("小龙小龙,今天几号", "今天几号"),
        ("小龙帮我放首歌", "帮我放首歌"),
        ("小隆,打开文件", "打开文件"),
        ("小龙 你好", "你好"),
        ("小龙小龙", None),
        ("今天天气好", None),
        ("小龙。", None),
    ]
    print("\n--- 指令提取 ---")
    for text, expected in cmd_tests:
        ctrl = SessionController()
        result = ctrl._try_extract_command(text)
        status = "OK" if result == expected else "FAIL"
        print(f"  [{status}] '{text}' -> '{result}' (期望'{expected}')")

    # 上下文关联测试
    print("\n\n=== 上下文关联测试 ===\n")
    results = []
    ctrl = SessionController()
    ctrl.set_callbacks(
        on_wake=lambda: results.append(("wake", None)),
        on_sleep=lambda: results.append(("sleep", None)),
        on_command=lambda cmd: results.append(("cmd", cmd)),
    )

    # 场景1: 休眠 → "小龙"唤醒 → (停顿) → "帮我放歌" 关联为指令
    print("场景1: 小龙(停顿)帮我放歌")
    results.clear()
    ctrl.state = SessionState.SLEEPING
    ctrl._pending_command = False
    ctrl.process_text("小龙。", is_final=True)
    assert ctrl.state == SessionState.ACTIVE, f"应为active, 实为{ctrl.state}"
    assert ctrl._pending_command == True, "应为pending"
    assert results == [("wake", None)], f"应为wake回调, 实为{results}"
    ctrl.process_text("帮我放一首歌曲。", is_final=True)
    assert ctrl.state == SessionState.PROCESSING, f"应为processing, 实为{ctrl.state}"
    assert results[-1] == ("cmd", "帮我放一首歌曲"), f"指令错误: {results}"
    print("  OK!")

    # 场景2: 活跃 → "小龙"(停顿) → "今天几号" 关联为指令
    print("场景2: 活跃态 小龙(停顿)今天几号")
    results.clear()
    ctrl.state = SessionState.ACTIVE
    ctrl._pending_command = False
    ctrl.process_text("小龙。", is_final=True)
    assert ctrl._pending_command == True
    ctrl.process_text("今天几号?", is_final=True)
    assert results[-1] == ("cmd", "今天几号"), f"指令错误: {results}"
    print("  OK!")

    # 场景3: "小龙帮我查天气" 一句搞定
    print("场景3: 小龙帮我查天气(一句)")
    results.clear()
    ctrl.state = SessionState.ACTIVE
    ctrl._pending_command = False
    ctrl.process_text("小龙帮我查天气。", is_final=True)
    assert results[-1] == ("cmd", "帮我查天气"), f"指令错误: {results}"
    print("  OK!")

    # 场景4: 休眠态 → "小龙帮我查天气" 唤醒+指令一步到位
    print("场景4: 休眠态 小龙帮我查天气(唤醒即指令)")
    results.clear()
    ctrl.state = SessionState.SLEEPING
    ctrl._pending_command = False
    ctrl.process_text("小龙帮我查天气。", is_final=True)
    assert ctrl.state == SessionState.PROCESSING
    assert results == [("cmd", "帮我查天气")], f"应无wake直接cmd, 实为{results}"
    print("  OK!")

    # 场景5: pending超时
    print("场景5: pending超时")
    results.clear()
    ctrl.state = SessionState.ACTIVE
    ctrl._pending_command = True
    ctrl._pending_time = time.time() - 10  # 10秒前
    ctrl.process_text("随便说的话", is_final=True)
    assert len(results) == 0, f"超时不应触发: {results}"
    assert ctrl._pending_command == False
    print("  OK!")

    # 场景6: "享龙享龙" 唤醒
    print("场景6: 享龙享龙 唤醒")
    results.clear()
    ctrl.state = SessionState.SLEEPING
    ctrl._pending_command = False
    ctrl.process_text("享龙享龙。", is_final=True)
    assert ctrl.state == SessionState.ACTIVE, f"应为active, 实为{ctrl.state}"
    print("  OK!")

    # 场景7: 活跃态 "龙,帮我播放" 截断前缀
    print("场景7: 龙,帮我播放(截断前缀)")
    results.clear()
    ctrl.state = SessionState.ACTIVE
    ctrl._pending_command = False
    ctrl.process_text("龙,帮我播放一首音乐。", is_final=True)
    assert results[-1] == ("cmd", "帮我播放一首音乐"), f"指令错误: {results}"
    print("  OK!")

    # 场景8: 活跃态 "龙。" 只有龙 → pending
    print("场景8: 龙。(截断唤醒)")
    results.clear()
    ctrl.state = SessionState.ACTIVE
    ctrl._pending_command = False
    ctrl.process_text("龙。", is_final=True)
    assert ctrl._pending_command == True
    ctrl.process_text("帮我查天气", is_final=True)
    assert results[-1] == ("cmd", "帮我查天气"), f"指令错误: {results}"
    print("  OK!")

    print("\n=== 全部测试通过 ===")
