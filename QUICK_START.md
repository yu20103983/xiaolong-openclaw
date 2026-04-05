# 🚀 快速开始

从零开始配置小龙语音助手，约 10 分钟。

## 前置条件

确保已安装：

- **Python 3.10+**：[下载地址](https://www.python.org/downloads/)，安装时勾选 "Add to PATH"
- **Node.js 18+**：[下载地址](https://nodejs.org/)
- **Git**：[下载地址](https://git-scm.com/)
- **麦克风 + 扬声器**（蓝牙耳机或电脑自带均可）

验证安装：

```bash
python --version   # 应显示 3.10+
node --version     # 应显示 18+
```

## 第一步：下载项目

```bash
git clone https://github.com/yourname/xiaolong-openclaw.git
cd xiaolong-openclaw
```

## 第二步：安装依赖

双击 `setup.bat`，或手动执行：

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Node.js 依赖
npm install

# 下载 ASR 模型（约 200MB）
download_models.bat
```

## 第三步：配置 OpenClaw

OpenClaw 是 AI Agent 框架，负责与大语言模型交互。

### 3.1 初始化配置

```bash
npx openclaw config
```

按提示选择：
1. **Gateway 位置** → 选 `Local (this machine)`
2. **LLM Provider** → 选择你的 AI 提供商（如 Anthropic）
3. **API Key** → 输入你的 API Key

### 3.2 启动 Gateway

```bash
npx openclaw gateway
```

看到类似输出表示成功：

```
🦞 Gateway listening on ws://127.0.0.1:18789
```

> **保持这个终端窗口运行**，Gateway 需要一直在后台。

## 第四步：修改配置（可选）

编辑 `src/config.py`，根据你的 OpenClaw 配置修改 provider 和 model：

```python
PI_PROVIDER = "anthropic"              # 你配置的 provider 名称
PI_MODEL = "claude-sonnet-4-20250514"  # 你要使用的模型
```

可用的 TTS 语音：

| 值 | 说明 |
|----|------|
| `xiaoxiao` | 女声，温柔（默认） |
| `yunxi` | 男声，自然 |
| `xiaoyi` | 女声，活泼 |
| `yunjian` | 男声，沉稳 |

## 第五步：启动语音助手

**打开新的终端窗口**（Gateway 终端保持运行），执行：

```bash
start.bat
```

看到以下输出表示启动成功：

```
==================================================
  🎧 小龙语音助手
  音频模式: 全双工(边说边听)
  '小龙小龙' 唤醒 | '小龙小龙退下' 休眠
  播放中说 '终止' 打断
==================================================

[Init] ✅ 就绪

等待语音输入...
```

## 开始使用

1. 对麦克风说 **"小龙小龙"** → 听到 "我在，请说"
2. 说 **"帮我查一下天气"** → AI 查询并语音回复
3. 说 **"小龙，今天天气怎么样"** → AI 查询并语音回复
4. 播报过程中说 **"终止"** → 打断当前播报
5. 说 **"小龙小龙，退下"** → 进入休眠

## 故障排查

### 没有检测到音频设备

```bash
python -X utf8 tests/test_devices.py
```

如果没有列出你的设备，检查系统音频设置。

### ASR 模型加载失败

确认模型已下载：

```
models/
├── sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/
│   ├── model.int8.onnx
│   └── tokens.txt
└── silero_vad.onnx
```

如果缺失，重新运行 `download_models.bat`。

### Gateway 连接失败

确认 Gateway 正在运行（另一个终端窗口）：

```bash
npx openclaw gateway
```

如果端口被占用，可以指定端口：

```bash
npx openclaw gateway --port 19000
```

然后在 `bin/gateway-bridge.js` 中修改默认 URL。

### 唤醒词不灵敏

在安静环境下，靠近麦克风，清晰地说 "小龙小龙"。可以先测试 ASR 是否正常：

```bash
python -X utf8 tests/test_asr.py
```

对着麦克风说话，看终端是否显示识别结果。
