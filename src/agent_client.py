"""
OpenClaw Agent 客户端
通过 Gateway Bridge 与 OpenClaw Gateway 通信，获得完整的 OpenClaw 工具和 Skills 生态。

架构:
  Python (agent_client.py) ←stdin/stdout JSON→ Node.js (gateway-bridge.js) ←WebSocket→ OpenClaw Gateway

对外暴露与 Pi RPC 兼容的接口（set_callbacks / prompt / prompt_async / abort / steer）。
"""

import subprocess
import json
import threading
import time
import os
from typing import Optional, Callable


class AgentClient:
    """OpenClaw Gateway Agent 客户端"""

    def __init__(self, working_dir: str = ".",
                 provider: str = "anthropic", model: str = "claude-sonnet-4-20250514",
                 auto_restart: bool = True, max_restarts: int = 3,
                 session_key: str = "agent:main:xiaolong",
                 gateway_url: str = "ws://127.0.0.1:18789"):
        self.working_dir = os.path.abspath(working_dir)
        self.provider = provider
        self.model = model
        self.auto_restart = auto_restart
        self.max_restarts = max_restarts
        self.session_key = session_key
        self.gateway_url = gateway_url
        self._restart_count = 0
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._health_thread: Optional[threading.Thread] = None
        self._running = False
        self._on_text_delta: Optional[Callable[[str], None]] = None
        self._on_response_complete: Optional[Callable[[str], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._current_response = ""
        self._response_event = threading.Event()
        self._connected_event = threading.Event()  # Bridge连接就绪信号
        self._lock = threading.Lock()
        self._steer_message: Optional[str] = None

    def _find_bridge(self) -> str:
        """查找 Gateway Bridge 脚本路径"""
        bridge = os.path.join(self.working_dir, "bin", "gateway-bridge.js")
        if os.path.exists(bridge):
            return os.path.abspath(bridge)
        raise FileNotFoundError(
            f"找不到 gateway-bridge.js: {bridge}"
        )

    def start(self):
        """启动 Gateway Bridge 进程"""
        self._start_process()
        if self.auto_restart and (self._health_thread is None or not self._health_thread.is_alive()):
            self._health_thread = threading.Thread(target=self._health_check, daemon=True)
            self._health_thread.start()

    def _start_process(self):
        """内部方法：启动 Bridge 子进程"""
        self._cleanup_proc()
        bridge = self._find_bridge()
        print(f"[Agent] Gateway Bridge: {bridge}")
        print(f"[Agent] Gateway URL: {self.gateway_url}")
        print(f"[Agent] Session: {self.session_key}")
        env = os.environ.copy()
        self._proc = subprocess.Popen(
            ["node", bridge,
             "--session", self.session_key,
             "--url", self.gateway_url],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.working_dir,
            env=env
        )
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_events, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        # 等待 Bridge 连接就绪（最多等5秒，通常1秒内完成）
        self._connected_event.clear()
        if not self._connected_event.wait(timeout=5):
            if self._proc.poll() is not None:
                raise RuntimeError("Gateway Bridge 启动失败")
            print("[Agent] 警告: Bridge未发送就绪信号，继续启动")
        else:
            print(f"[Agent] OpenClaw Gateway 已连接")

    def _cleanup_proc(self):
        """安全清理旧的子进程及其管道"""
        old_proc = self._proc
        self._proc = None
        if old_proc is None:
            return
        for pipe in (old_proc.stdin, old_proc.stdout, old_proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass
        try:
            old_proc.terminate()
            old_proc.wait(timeout=3)
        except Exception:
            try:
                old_proc.kill()
            except Exception:
                pass

    def _send(self, cmd: dict):
        """发送命令到 Bridge"""
        proc = self._proc
        if proc and proc.stdin and proc.poll() is None:
            try:
                line = json.dumps(cmd, ensure_ascii=False).encode('utf-8') + b"\n"
                proc.stdin.write(line)
                proc.stdin.flush()
            except OSError as e:
                print(f"[Agent] 发送失败: {e}")

    def _read_stderr(self):
        """持续读取 Bridge 的 stderr 输出，防止 pipe buffer 满导致阻塞"""
        while self._running and self._proc:
            try:
                raw = self._proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode('utf-8', errors='replace').strip()
                if line:
                    print(f"[Bridge] {line}")
            except Exception:
                break

    def _read_events(self):
        """持续读取 Bridge 输出的事件"""
        while self._running and self._proc:
            try:
                raw = self._proc.stdout.readline()
                if not raw:
                    if self._running:
                        print("[Agent] Bridge 输出流已关闭")
                    break
                line = raw.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    self._handle_event(event)
                except json.JSONDecodeError:
                    pass
            except Exception as e:
                if self._running:
                    print(f"[Agent] 读取错误: {e}")
                break

    def _handle_event(self, event: dict):
        """处理事件（Pi RPC 兼容格式）"""
        event_type = event.get("type", "")

        if event_type == "message_update":
            ame = event.get("assistantMessageEvent", {})
            if ame.get("type") == "text_delta":
                delta = ame.get("delta", "")
                self._current_response += delta
                if self._on_text_delta:
                    self._on_text_delta(delta)

        elif event_type == "bridge_ready":
            # Bridge已连接Gateway，标记就绪
            self._connected_event.set()

        elif event_type == "tool_execution_start":
            if not self._current_response.strip() and self._on_text_delta:
                tool_name = event.get("toolName", "")
                hint = "好的，我来处理一下"
                self._current_response += hint
                self._on_text_delta(hint)

        elif event_type == "agent_end":
            response = self._current_response.strip()
            # 过滤 gateway 心跳响应
            if response and response.replace('HEARTBEAT_OK', '').strip() == '':
                self._current_response = ""
                return
            if response and self._on_response_complete:
                self._on_response_complete(response)
            self._response_event.set()

        elif event_type == "response":
            if not event.get("success", True):
                error = event.get("error", "Unknown error")
                if self._on_error:
                    self._on_error(error)

    def set_callbacks(self,
                      on_text_delta: Optional[Callable[[str], None]] = None,
                      on_response_complete: Optional[Callable[[str], None]] = None,
                      on_error: Optional[Callable[[str], None]] = None):
        """设置事件回调"""
        self._on_text_delta = on_text_delta
        self._on_response_complete = on_response_complete
        self._on_error = on_error

    def prompt(self, message: str, timeout: float = 60) -> Optional[str]:
        """发送提示并等待完整响应"""
        self._current_response = ""
        self._response_event.clear()
        self._send({"type": "prompt", "message": message})
        if self._response_event.wait(timeout=timeout):
            return self._current_response.strip()
        else:
            print("[Agent] 等待响应超时")
            return None

    def prompt_async(self, message: str):
        """异步发送提示（不等待响应）"""
        self._current_response = ""
        self._response_event.clear()
        self._send({"type": "prompt", "message": message})

    def abort(self):
        """中止当前操作"""
        self._send({"type": "abort"})

    def _health_check(self):
        """定期检查进程是否存活，崩溃时自动重启"""
        while self._running:
            time.sleep(3)
            if not self._running:
                break
            if self._proc and self._proc.poll() is not None:
                exit_code = self._proc.poll()
                print(f"[Agent] Bridge 进程已退出 (code={exit_code})")
                if self._restart_count < self.max_restarts:
                    self._restart_count += 1
                    print(f"[Agent] 自动重启 ({self._restart_count}/{self.max_restarts})...")
                    try:
                        self._start_process()
                        self._restart_count = 0
                        print("[Agent] 重启成功")
                    except Exception as e:
                        print(f"[Agent] 重启失败: {e}")
                else:
                    print("[Agent] 已达最大重启次数，停止重试")
                    break

    def save_steer(self, message: str):
        """保存 steer 消息（Gateway 模式下通过 workspace 文件配置 system prompt）"""
        self._steer_message = message

    def stop(self):
        """停止 Bridge 进程"""
        self._running = False
        self._cleanup_proc()
        print("[Agent] OpenClaw Gateway Bridge 已停止")

    @property
    def is_running(self) -> bool:
        return self._running and self._proc is not None and self._proc.poll() is None


if __name__ == "__main__":
    print("=== OpenClaw Gateway Agent 测试 ===")
    client = AgentClient(working_dir=".")

    def on_delta(delta):
        print(delta, end="", flush=True)

    def on_complete(text):
        print(f"\n\n[完整响应] 共 {len(text)} 字符")

    client.set_callbacks(on_text_delta=on_delta, on_response_complete=on_complete)
    client.start()

    response = client.prompt("你好，请用一句话介绍自己")
    print(f"\n响应: {response}")

    client.stop()
