# -*- coding: utf-8 -*-
"""acp_channel —— agent-hub ↔ hermes-agent 的 ACP（Agent Client Protocol）直通通道。

定位（2026-09-20，冒烟验证见 test_acp_smoke.py，全链路 8s 真实回复）
----------------------------------------------------------------------
hermes-agent 是本机完整 agent runtime（skills 目录 + skill-router + optional-skills），
自带 hermes-acp.exe 以 ACP stdio JSON-RPC 对外服务。本模块把该通道封装成
agent-hub 可调用的"带技能的专家 Agent"：

    from acp_channel import HermesAcpClient
    with HermesAcpClient() as hermes:
        reply = hermes.ask("列出你能用的技能")

通道特性（与 L1 裸模型的本质区别）
--------------------------------
- 对方是完整 Agent：能自主调用它自己的技能库（pdf/docx/xlsx/notion/arxiv...）
- 会话可复用：同一 client 多次 ask 共享 session（对话连续）
- 密钥零接触：hermes 的模型密钥由它自己的 .env 持有，hub 不读不传
- 有界超时：单次 RPC 与整条 ask 都有超时，进程树可强杀

认证：initialize 后若服务端声明 authMethods 且本机 provider 已配置
（hermes/.env 的 DEEPSEEK_API_KEY 等），authenticate 一次即过；未配置时
ask() 抛 RuntimeError（由上层按"该 Agent 缺席"处理，与 real adapter 约定一致）。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", r"C:\Users\MVW\AppData\Local\hermes"))
HERMES_ACP_EXE = HERMES_HOME / "bin" / "hermes-acp.exe"
AGENT_DIR = HERMES_HOME / "hermes-agent"

DEFAULT_RPC_TIMEOUT = 120        # 单个 RPC
DEFAULT_ASK_TIMEOUT = 600        # 整条 ask（agent 可能跑多个工具步骤）


class AcpError(RuntimeError):
    """ACP 通道错误（进程起不来 / 握手失败 / 超时 / 认证缺失）。"""


class AcpChannel:
    """极简 ACP stdio 客户端：换行分隔 JSON-RPC，后台线程读响应。"""

    def __init__(self, exe: Path, cwd: Path) -> None:
        self._id = 0
        self.updates: list[dict] = []
        self.proc = subprocess.Popen(
            [str(exe)],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._responses: dict[int, dict] = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    self._responses[msg["id"]] = msg
                    ev = self._pending.pop(msg["id"], None)
                if ev is not None:
                    ev.set()
            elif msg.get("method") == "session/update":
                self.updates.append(msg.get("params") or {})

    def rpc(self, method: str, params: dict, timeout: int = DEFAULT_RPC_TIMEOUT) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
            ev = threading.Event()
            self._pending[rid] = ev
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            with self._lock:
                self._pending.pop(rid, None)
            raise AcpError(f"ACP 进程不可写（可能已退出）: {e}") from e
        if not ev.wait(timeout):
            raise AcpError(f"RPC {method} 超时（{timeout}s）")
        with self._lock:
            msg = self._responses.pop(rid)
        if "error" in msg:
            raise AcpError(f"RPC {method} 错误: {msg['error']}")
        return msg.get("result") or {}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class HermesAcpClient:
    """高层接口：一条进程一个会话，ask() 即问即答。"""

    def __init__(self, exe: Path | None = None, agent_dir: Path | None = None,
                 workdir: Path | None = None, ask_timeout: int = DEFAULT_ASK_TIMEOUT) -> None:
        self._exe = Path(exe or HERMES_ACP_EXE)
        self._agent_dir = Path(agent_dir or AGENT_DIR)
        self._workdir = Path(workdir or tempfile.mkdtemp(prefix="acp_hub_"))
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._ask_timeout = ask_timeout
        self._ch: AcpChannel | None = None
        self._session_id: str | None = None

    # ---- 生命周期 ----
    def _ensure_connected(self) -> AcpChannel:
        if self._ch is not None and self._ch.proc.poll() is None:
            return self._ch
        if not self._exe.is_file():
            raise AcpError(f"hermes-acp 不存在: {self._exe}")
        if not self._agent_dir.is_dir():
            raise AcpError(f"hermes-agent 目录不存在: {self._agent_dir}")
        ch = AcpChannel(self._exe, self._agent_dir)
        try:
            info = ch.rpc("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                       "terminal": False},
                "clientInfo": {"name": "agent-hub", "version": "1.0"},
            })
            methods = info.get("authMethods") or []
            if isinstance(methods, dict):
                methods = methods.get("methods") or []
            if methods:
                mid = methods[0].get("id") or methods[0].get("methodId")
                try:
                    ch.rpc("authenticate", {"methodId": mid}, timeout=30)
                except AcpError as e:
                    raise AcpError(
                        f"hermes 认证失败（method={mid}）：请先在 hermes 侧配置模型密钥"
                        f"（hermes/.env 的 *_API_KEY）") from e
            self._ch = ch
            self._session_id = None      # 重连后会话作废
            return ch
        except Exception:
            ch.close()
            raise

    def _ensure_session(self, ch: AcpChannel) -> str:
        if self._session_id:
            return self._session_id
        sess = ch.rpc("session/new", {"cwd": str(self._workdir), "mcpServers": []})
        sid = sess.get("sessionId") or sess.get("session_id")
        if not sid:
            raise AcpError(f"session/new 未返回 sessionId: {sess}")
        self._session_id = sid
        return sid

    # ---- 对外 ----
    def ask(self, prompt: str, timeout: int | None = None) -> str:
        """发一条用户消息，返回 hermes 的最终文本回复（agent_message 分片拼接）。"""
        ch = self._ensure_connected()
        sid = self._ensure_session(ch)
        n_before = len(ch.updates)
        ch.rpc("session/prompt", {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": prompt}],
        }, timeout=timeout or self._ask_timeout)
        parts: list[str] = []
        for u in ch.updates[n_before:]:
            upd = u.get("update") or {}
            kind = upd.get("sessionUpdate") or upd.get("type")
            if kind == "agent_message_chunk":
                content = upd.get("content") or {}
                parts.append(str(content.get("text") or ""))
        return "".join(parts).strip()

    def close(self) -> None:
        if self._ch is not None:
            self._ch.close()
            self._ch = None
            self._session_id = None

    def __enter__(self) -> "HermesAcpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def hermes_available() -> bool:
    """hermes ACP 通道是否可用（本机装了 hermes-agent）。"""
    return HERMES_ACP_EXE.is_file() and AGENT_DIR.is_dir()
