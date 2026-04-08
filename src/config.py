"""
小龙语音助手 - 全局配置
"""

# ============ 音频设备 ============
# 设为 None 时自动检测蓝牙/本地设备
A2DP_ID = None            # 输出设备 ID (None=自动检测)
A2DP_SR = None            # 输出采样率 (None=自动检测)
HFP_IN = None             # 输入设备 ID (None=自动检测)
HFP_IN_SR = None          # 输入采样率 (None=自动检测)
SPLIT_IO = False          # 分离模式：本地麦克风输入 + 蓝牙A2DP输出
DUPLEX_MODE = None        # 全双工模式: True/False/None(自动检测)
ECHO_CANCEL = "mute"      # 回音消除模式: "aec"(声学回音消除,播放时仍可识别语音) / "mute"(播放时静音麦克风) / False(关闭)
HFP_DUPLEX = True         # HFP全双工模式: 输入输出都走HFP(音质降低但支持边说边听)
PREFER_LOCAL = False      # 优先本地设备（跳过蓝牙检测）

# ============ TTS ============
TTS_ENGINE = "edge"           # TTS 引擎：edge (在线, 音质好) / local (离线, 快)
TTS_VOICE = "xiaoxiao"        # edge-tts 语音：xiaoxiao/yunxi/xiaoyi/yunjian
TTS_RATE = "+10%"             # edge-tts 语速
TTS_LOCAL_MODEL = "matcha-zh-baker"  # 本地备选模型（edge失败时自动回退）

# ============ 播放管线 ============
MAX_MERGE_CLAUSES = 2     # 最多合并逗号短句数（提升语气连贯性）
INPUT_SILENCE_TIMEOUT = 4.0  # 静音超时后发送指令（秒）—— 长超时（默认）
INPUT_QUICK_TIMEOUT = 1.5   # 短超时：输入看起来已经完整时用

# ============ 会话 ============
AUTO_SLEEP_TIMEOUT = 0    # 无活动自动休眠秒数 (0=禁用)
CONTINUOUS_SILENCE_TIMEOUT = 30  # 连续对话模式：用户沉默超时秒数

# ============ Agent (OpenClaw) ============
PI_WORKING_DIR = "."                   # OpenClaw 工作目录
PI_PROVIDER = "anthropic"              # OpenClaw provider (需与 openclaw config 一致)
PI_MODEL = "claude-sonnet-4-20250514"  # 模型名称 (需与 openclaw config 一致)
