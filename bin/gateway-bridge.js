#!/usr/bin/env node
/**
 * OpenClaw Gateway Bridge
 * 连接 OpenClaw Gateway WebSocket，对外暴露 Pi RPC 兼容的 stdin/stdout JSON 协议。
 * 这样 Python 端的 agent_client.py 无需修改通信协议。
 *
 * 用法: node gateway-bridge.js [--session <key>] [--url <ws-url>]
 */

const WebSocket = require("ws");
const crypto = require("crypto");
const readline = require("readline");

// --- 配置 ---
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf("--" + name);
  return idx >= 0 && idx + 1 < args.length ? args[idx + 1] : defaultVal;
}
const WS_URL = getArg("url", "ws://127.0.0.1:18789");
const SESSION_KEY = getArg("session", "agent:main:xiaolong");

// --- Base64URL 编码 ---
function base64UrlEncode(buf) {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// --- 设备身份 (Ed25519) ---
const { publicKey, privateKey } = crypto.generateKeyPairSync("ed25519");
const pubDer = publicKey.export({ type: "spki", format: "der" });
// Ed25519 SPKI DER 前缀固定 12 字节，后面 32 字节是原始公钥
const ED25519_SPKI_PREFIX_LEN = 12;
const pubRaw = pubDer.subarray(ED25519_SPKI_PREFIX_LEN);
const deviceId = crypto.createHash("sha256").update(pubRaw).digest("hex");
const pubB64Url = base64UrlEncode(pubRaw);

// --- 状态 ---
let ws = null;
let connected = false;
let currentResponse = "";
let idempotencyCounter = 0;
let subscribedSession = null;
let systemPrompt = null;  // steer 消息作为 system prompt
let promptActive = false;      // 是否有活跃的 prompt
let agentEndEmitted = false;   // 当前 prompt 是否已发送 agent_end
let lastDeltaAt = 0;           // 最后一次收到 text_delta 的时间
let promptSentAt = 0;          // prompt 发送时间
let stallCheckTimer = null;    // 停滞检测定时器
const STALL_CHECK_INTERVAL = 15000;  // 每15秒检查一次
const STALL_TIMEOUT = 120000;        // 2分钟无任何text/tool事件则强制结束
const FIRST_RESPONSE_TIMEOUT = 60000; // prompt发出后60秒内必须收到第一个text_delta

// --- 输出 (Pi RPC 兼容事件) ---
function emit(event) {
  process.stdout.write(JSON.stringify(event) + "\n");
}

// --- WebSocket 连接 ---
function connectGateway() {
  ws = new WebSocket(WS_URL);

  ws.on("open", () => {
    process.stderr.write("[bridge] WebSocket connected\n");
  });

  ws.on("message", (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return;
    }
    handleGatewayMessage(msg);
  });

  ws.on("close", () => {
    process.stderr.write("[bridge] WebSocket closed\n");
    connected = false;
    // 自动重连
    setTimeout(connectGateway, 2000);
  });

  ws.on("error", (err) => {
    process.stderr.write(`[bridge] WebSocket error: ${err.message}\n`);
  });
}

function handleGatewayMessage(msg) {
  // 1. Challenge → 签名 → connect
  if (msg.type === "event" && msg.event === "connect.challenge") {
    const nonce = msg.payload.nonce;
    const ts = Date.now();
    const platform = process.platform.toLowerCase();
    // v3 payload: pipe-separated fields
    const signPayload = [
      "v3", deviceId, "cli", "cli", "operator",
      "operator.read,operator.write,operator.admin",
      String(ts), "", nonce, platform, ""
    ].join("|");
    const sig = base64UrlEncode(crypto.sign(null, Buffer.from(signPayload, "utf8"), privateKey));

    ws.send(JSON.stringify({
      type: "req", id: "__connect", method: "connect",
      params: {
        minProtocol: 3, maxProtocol: 3,
        client: { id: "cli", version: "1.0.0", platform: process.platform, mode: "cli" },
        role: "operator",
        scopes: ["operator.read", "operator.write", "operator.admin"],
        caps: [], commands: [], permissions: {},
        auth: {},
        device: { id: deviceId, publicKey: pubB64Url, signature: sig, signedAt: ts, nonce },
      },
    }));
    return;
  }

  // 2. Connect 结果
  if (msg.type === "res" && msg.id === "__connect") {
    if (msg.ok) {
      connected = true;
      process.stderr.write("[bridge] Gateway authenticated\n");
      // 通知 Python 端已就绪
      emit({ type: "bridge_ready" });
      process.stderr.write(`[bridge] Ready, session=${SESSION_KEY}\n`);
    } else {
      process.stderr.write(`[bridge] Connect failed: ${JSON.stringify(msg.error)}\n`);
      process.exit(1);
    }
    return;
  }

  // 4. chat.send 结果
  if (msg.type === "res" && msg.id && msg.id.startsWith("prompt_")) {
    emit({ type: "response", command: "prompt", success: msg.ok });
    if (msg.ok) {
      emit({ type: "agent_start" });
      emit({ type: "turn_start" });
    } else {
      // prompt 发送失败，立即结束
      process.stderr.write(`[bridge] prompt rejected: ${JSON.stringify(msg.error)}\n`);
      if (!currentResponse) currentResponse = `抱歉，指令发送失败`;
      emitAgentEnd();
    }
    return;
  }

  // 4b. stall_check 响应（已废弃，chat.status API 不存在，直接忽略）
  if (msg.type === "res" && msg.id === "__stall_check") {
    return;
  }

  // 5. 流式事件
  if (msg.type === "event") {
    handleStreamEvent(msg);
  }
}

// --- 处理流式事件 → Pi RPC 格式 ---
function handleStreamEvent(msg) {
  const evt = msg.event || "";
  const p = msg.payload || {};

  // Agent 流式文本 (event: "agent", stream: "assistant")
  if (evt === "agent" && p.stream === "assistant" && p.sessionKey === SESSION_KEY) {
    // 没有活跃 prompt 或已结束 → 丢弃 (防止 bootstrap/残留事件)
    if (!promptActive || agentEndEmitted) return;
    const delta = p.data?.delta || "";
    // 过滤心跳相关文本 (可能拆成多个 delta)
    if (!delta || /^\s*HEARTBEAT_?O?K?\s*$/.test(delta) || /^\s*NO_?\s*$/.test(delta)) return;
    currentResponse += delta;
    lastDeltaAt = Date.now();
    emit({
      type: "message_update",
      assistantMessageEvent: { type: "text_delta", delta },
    });
    return;
  }

  // Agent 工具调用 (event: "agent", stream: "tool")
  if (evt === "agent" && p.stream === "tool" && p.sessionKey === SESSION_KEY) {
    lastDeltaAt = Date.now();  // 工具事件也重置停滞计时
    const phase = p.data?.phase || "";
    if (phase === "start") {
      emit({ type: "tool_execution_start", toolName: p.data?.name || "" });
    }
    return;
  }

  // Agent 生命周期 (event: "agent", stream: "lifecycle")
  if (evt === "agent" && p.stream === "lifecycle" && p.sessionKey === SESSION_KEY) {
    const phase = p.data?.phase || "";
    if (phase === "end" || phase === "error" || phase === "timeout") {
      if (phase !== "end") {
        process.stderr.write(`[bridge] Agent lifecycle ${phase}\n`);
        if (!currentResponse) {
          currentResponse = `抱歉，处理超时了，请再说一次`;
        }
      }
      emitAgentEnd();
    }
    return;
  }

  // chat 状态事件（final / error / aborted）
  if (evt === "chat" && p.sessionKey === SESSION_KEY) {
    if (p.state === "error" || p.state === "aborted") {
      const errMsg = p.errorMessage || p.state;
      process.stderr.write(`[bridge] Chat ${p.state}: ${errMsg}\n`);
      if (!currentResponse) {
        currentResponse = `抱歉，出了点问题：${errMsg}`;
      }
      emitAgentEnd();
      return;
    }
    if (p.state === "final") {
      emitAgentEnd();
    }
  }
}

// --- stdin 读取 (Pi RPC 命令) ---
const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", (line) => {
  let cmd;
  try {
    cmd = JSON.parse(line.trim());
  } catch {
    return;
  }

  if (cmd.type === "prompt") {
    handlePrompt(cmd.message);
  } else if (cmd.type === "steer") {
    // 保存 system prompt，在下一次 prompt 时拼接到用户消息前
    systemPrompt = cmd.message || null;
    process.stderr.write(`[bridge] steer saved (${systemPrompt ? systemPrompt.length : 0} chars)\n`);
  } else if (cmd.type === "abort") {
    handleAbort();
  }
});

rl.on("close", () => {
  if (ws) ws.close();
  process.exit(0);
});

// 统一的 agent_end 发送（防重复）
function emitAgentEnd() {
  if (agentEndEmitted) return;  // 已发过，不重复
  agentEndEmitted = true;
  promptActive = false;
  stopStallCheck();
  // 强制发 abort 确保 gateway 释放 session lane
  if (connected && ws) {
    ws.send(JSON.stringify({
      type: "req", id: "__cleanup_abort",
      method: "chat.abort",
      params: { sessionKey: SESSION_KEY },
    }));
  }
  // 清理心跳/干扰文本
  currentResponse = currentResponse.replace(/HEARTBEAT_?O?K?/g, "").replace(/\bNO_/g, "").trim();
  if (currentResponse) {
    emit({
      type: "agent_end",
      messages: [{ role: "assistant", content: [{ type: "text", text: currentResponse }] }],
    });
  }
  currentResponse = "";
}

function handlePrompt(message) {
  if (!connected || !ws) {
    emit({ type: "response", command: "prompt", success: false, error: "Not connected to Gateway" });
    return;
  }

  currentResponse = "";
  promptActive = true;
  agentEndEmitted = false;
  lastDeltaAt = Date.now();
  promptSentAt = Date.now();
  startStallCheck();
  const idKey = `prompt_${++idempotencyCounter}_${Date.now()}`;

  // 将 system prompt 拼接到第一次用户消息前，后续只发用户消息
  let fullMessage = message;
  if (systemPrompt) {
    fullMessage = `[System Prompt]\n${systemPrompt}\n[/System Prompt]\n\n用户说: ${message}`;
    systemPrompt = null;  // 只发一次，gateway 会保持会话上下文
    process.stderr.write(`[bridge] first prompt with system prompt\n`);
  }

  ws.send(JSON.stringify({
    type: "req",
    id: idKey,
    method: "chat.send",
    params: {
      sessionKey: SESSION_KEY,
      message: fullMessage,
      idempotencyKey: idKey,
    },
  }));
}

function handleAbort() {
  if (!connected || !ws) return;
  ws.send(JSON.stringify({
    type: "req",
    id: "__abort",
    method: "chat.abort",
    params: { sessionKey: SESSION_KEY },
  }));
}

// --- 停滞检测：定期查询 chat 状态，防止 gateway 超时但未广播结束事件 ---
function startStallCheck() {
  stopStallCheck();
  stallCheckTimer = setInterval(() => {
    if (!promptActive || agentEndEmitted) { stopStallCheck(); return; }
    const now = Date.now();
    const idleMs = now - lastDeltaAt;
    const sincePrompt = now - promptSentAt;
    // 首次响应超时：prompt发出后N秒内没收到任何text_delta
    if (!currentResponse && sincePrompt > FIRST_RESPONSE_TIMEOUT) {
      process.stderr.write(`[bridge] first response timeout: ${Math.round(sincePrompt/1000)}s since prompt, no text received\n`);
      currentResponse = `抱歉，处理超时了，请再说一次`;
      emitAgentEnd();
      return;
    }
    // 停滞超时：收到过内容但之后N秒无任何事件
    if (idleMs < STALL_TIMEOUT) return;
    process.stderr.write(`[bridge] stall timeout: ${Math.round(idleMs/1000)}s idle, forcing agent_end\n`);
    if (!currentResponse) currentResponse = `抱歉，处理超时了，请再说一次`;
    emitAgentEnd();
  }, STALL_CHECK_INTERVAL);
}

function stopStallCheck() {
  if (stallCheckTimer) { clearInterval(stallCheckTimer); stallCheckTimer = null; }
}

// --- 启动 ---
process.stderr.write(`[bridge] Connecting to ${WS_URL}, session=${SESSION_KEY}\n`);
connectGateway();
