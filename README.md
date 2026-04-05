# 🐉 小龙语音助手

<p align="center">
  <img src="xiaolong_preview.png" width="200" alt="小龙">
</p>

基于唤醒词的中文语音 AI 助手，通过 [OpenClaw](https://github.com/nicholasgriffintn/openclaw) Gateway 获得完整的工具调用和 Skills 生态能力。

**说"小龙小龙"唤醒 → 语音发指令 → AI 执行并语音回复**

## ✨ 特性

- 🎤 **离线唤醒词**：SenseVoice + Silero VAD，纯本地运行，无需联网
- 🗣️ **自然语音交互**：支持连续对话、语音打断、上下文指令拼接
- 🔧 **完整 AI 能力**：通过 OpenClaw 获得文件操作、命令执行、联网搜索、编程等能力
- 🎵 **内置 Skills**：音乐播放（B站搜索下载）、天气查询等，可扩展
- 🎧 **蓝牙支持**：自动检测蓝牙耳机，支持全双工/半双工自适应
- ⚡ **流式响应**：TTS 边合成边播放，首字延迟低

## 📐 架构

```
麦克风 → [Silero VAD + SenseVoice ASR] → 文字
                                           ↓
                                    [唤醒词/指令检测]
                                           ↓
                              [OpenClaw Gateway Agent]
                                           ↓
                                    [Edge TTS 合成]
                                           ↓
                                        扬声器
```

## 🚀 快速开始

详见 [QUICK_START.md](QUICK_START.md)

### 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | >= 3.10 | 推荐 3.12+ |
| Node.js | >= 18 | OpenClaw 运行时 |
| 麦克风 + 扬声器 | - | 蓝牙耳机或本地音频设备 |

### 一键安装

```bash
git clone https://github.com/yourname/xiaolong-openclaw.git
cd xiaolong-openclaw

# 安装依赖 + 下载模型
setup.bat
```

### 配置 OpenClaw

首次使用需配置 OpenClaw Gateway：

```bash
# 初始化 OpenClaw 配置（设置 API Key 等）
npx openclaw config

# 启动 Gateway（保持运行）
npx openclaw gateway
```

> **注意**：需要配置 LLM 提供商的 API Key（如 Anthropic、OpenAI 等）。
> 运行 `npx openclaw config` 会引导你完成配置。

### 启动

```bash
# 新终端，启动语音助手
start.bat
```

说 **"小龙小龙"** 唤醒，然后说指令即可。

## 🎯 使用方式

| 操作 | 说法示例 |
|------|---------|
| 唤醒 | "小龙小龙" |
| 发指令 | "小龙，帮我查一下天气" |
| 连续对话 | "小龙，我们聊聊天"（agent 自动开启） |
| 打断播报 | 说 "终止" |
| 休眠 | "小龙小龙，退下" |

### 指令输入

- 说完指令后，等待 4 秒静音自动发送
- 多段输入会自动拼接（说完一段后继续说，会合并后一起发送）
- 说 "好了" 可立即发送

### 连续对话

Agent 会根据场景自动开启连续对话模式（如聊天、口语练习等），开启后不需要说唤醒词，直接说话即可。

## ⚙️ 配置

编辑 `src/config.py`：

```python
# 音频设备（None = 自动检测）
A2DP_ID = None          # 输出设备 ID
HFP_IN = None           # 输入设备 ID
DUPLEX_MODE = None       # 全双工模式: True/False/None(自动)

# TTS
TTS_VOICE = "xiaoxiao"   # 语音: xiaoxiao/yunxi/xiaoyi/yunjian
TTS_RATE = "+10%"        # 语速

# Agent
PI_PROVIDER = "anthropic"              # OpenClaw provider
PI_MODEL = "claude-sonnet-4-20250514"  # 模型名称

# 会话
INPUT_SILENCE_TIMEOUT = 4.0    # 静音超时（秒）
CONTINUOUS_SILENCE_TIMEOUT = 30 # 连续对话沉默超时（秒）
```

## 🎵 Skills

### 音乐播放

内置 music-player skill，从 B 站搜索下载音频并播放：

```bash
# 安装到 OpenClaw workspace（首次需要）
mkdir -p ~/.openclaw/workspace/skills/music-player
cp .pi/skills/music-player/* ~/.openclaw/workspace/skills/music-player/

# 需要 yt-dlp
pip install yt-dlp
```

安装后说 "小龙，播放一首xxx" 即可。

### 添加自定义 Skill

1. 在 `~/.openclaw/workspace/skills/your-skill/` 下创建 `SKILL.md` 和脚本
2. 运行 `npx openclaw skills list` 确认加载

## 🏗️ 项目结构

```
├── src/
│   ├── main.py              # 主程序：唤醒→识别→Agent→TTS 流水线
│   ├── config.py             # 全局配置
│   ├── session_controller.py # 会话状态机：唤醒词检测、指令分发
│   ├── agent_client.py       # OpenClaw Gateway 客户端
│   ├── asr_engine.py         # ASR 引擎：VAD + SenseVoice
│   ├── tts_engine.py         # TTS 引擎：Edge TTS + 缓存
│   └── audio_io.py           # 音频 I/O：设备检测、录音、重采样
├── bin/
│   └── gateway-bridge.js     # Node.js Bridge：Python ↔ OpenClaw Gateway
├── tests/                    # 测试脚本
├── setup.bat                 # 一键安装
├── start.bat                 # 启动脚本
└── download_models.bat       # 模型下载
```

## 🔧 开发

### 测试单个组件

```bash
# 测试音频设备检测
python -X utf8 tests/test_devices.py

# 测试 ASR（麦克风实时识别）
python -X utf8 tests/test_asr.py

# 测试 TTS（合成并播放）
python -X utf8 tests/test_tts.py

# 测试唤醒词匹配
python -X utf8 src/session_controller.py
```

### 日志格式

运行时终端会显示清晰的对话日志：

```
==================================================
[用户] 帮我查一下今天的天气
==================================================
好的，我来查一下。深圳今天局部多云，气温25度...
--------------------------------------------------
[小龙] 好的，我来查一下。深圳今天局部多云，气温25度，适合出门。
--------------------------------------------------
```

## 📝 常见问题

**Q: 启动后没有声音？**
检查音频设备：`python -X utf8 tests/test_devices.py`，确认输入输出设备正确。

**Q: OpenClaw Gateway 连接失败？**
确保 Gateway 已启动：`npx openclaw gateway`，默认端口 18789。

**Q: 模型下载失败？**
设置代理后重试：
```bash
set HTTPS_PROXY=http://127.0.0.1:7890
download_models.bat
```

**Q: 唤醒词识别不灵敏？**
可在 `src/session_controller.py` 中调整 `_XIAO_CHARS` 和 `_LONG_CHARS` 的近音字集合。

## 📄 License

[MIT](LICENSE)
