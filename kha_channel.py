"""Kimi Hosted Agents (KHA) 通道 —— agent-hub L2 第四条 Agent 直通通道。

与 hermes ACP 通道同定位：对方是平台托管的完整 Agent（模型 + system prompt
+ 工具/技能/插件 + 云沙箱执行环境），不是裸模型。评审时技能随行。

API 契约来源：platform.kimi.com/docs（hosted-agents，2026-09-20 查证）
- 基址 https://api.moonshot.cn，请求头 `Authorization: Bearer <key>`
  + `kimi-api-version: 2026-09-01-beta`
- 智能体/环境/会话均为 REST 资源；消息经 user.message 事件下发
- 轮询会话状态到 idle 后，从协调线程历史事件收集 agent.message 文本

准入门槛（重要）：托管智能体 Beta 仅对**企业认证**账号开放。
未开通时 GET /v1/agents 返回 403 permission_denied_error，
kha_available()=False，adapter 构造抛 RuntimeError（与其他 adapter 同约定）。
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

API_VERSION = "2026-09-01-beta"
DEFAULT_BASE = "https://api.moonshot.cn"


class KhaError(RuntimeError):
    """KHA 调用失败。403 且含 enterprise 特征时提示企业认证门槛。"""


def _friendly(status: int, body: str) -> str:
    if status == 403 and "hosted-agents enterprise" in body:
        return ("[kha] 账号未开通托管智能体（需要 Kimi 开放平台企业认证，"
                "Beta 仅对企业认证用户开放；控制台申请或联系销售）")
    return f"[kha] HTTP {status}: {body[:300]}"


class KhaClient:
    """KHA REST 客户端：环境 → 会话 → 发消息 → 等 idle → 取文本。"""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE,
                 timeout: int = 120) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {api_key}",
            "kimi-api-version": API_VERSION,
        })

    def _req(self, method: str, path: str, **kw) -> Any:
        r = self.s.request(method, f"{self.base}{path}",
                           timeout=self.timeout, **kw)
        if r.status_code == 204:
            return None
        if r.status_code >= 400:
            raise KhaError(_friendly(r.status_code, r.text))
        return r.json()

    # ---- 资源：智能体 / 环境 -------------------------------------------------
    def list_agents(self) -> list[dict]:
        data = self._req("GET", "/v1/agents")
        return data.get("items", [])

    def create_agent(self, name: str, model: str = "kimi-k3",
                     system: str = "") -> dict:
        """创建智能体。技能/插件挂载字段以「创建与管理智能体」正式契约为准，
        本方法只封装最常用的 name/model/system 三元组。"""
        body: dict = {"name": name, "model": {"id": model}}
        if system:
            body["system"] = system
        return self._req("POST", "/v1/agents", json=body)

    def list_environments(self) -> list[dict]:
        data = self._req("GET", "/v1/environments")
        return data.get("items", [])

    def ensure_environment(self) -> str:
        """有现成环境直接用；否则创建 type=cloud 默认环境。返回环境 id。"""
        envs = self.list_environments()
        if envs:
            return envs[0]["id"]
        env = self._req("POST", "/v1/environments", json={
            "name": "agent-hub 评审环境",
            "description": "agent-hub 多AI评审的 KHA 云沙箱",
            "config": {"type": "cloud"},
        })
        return env["id"]

    # ---- 会话生命周期 --------------------------------------------------------
    def create_session(self, agent_id: str, environment_id: str,
                       title: str = "agent-hub review") -> str:
        sess = self._req("POST", "/v1/sessions", json={
            "agent_id": agent_id,
            "environment_id": environment_id,
            "title": title,
        })
        return sess["id"]

    def send_message(self, session_id: str, text: str) -> None:
        self._req("POST", f"/v1/sessions/{session_id}/events", json={
            "events": [{
                "type": "user.message",
                "data": {"content": [{"type": "text", "text": text}]},
            }],
        })

    def session_status(self, session_id: str) -> str:
        sess = self._req("GET", f"/v1/sessions/{session_id}")
        return sess.get("status", "unknown")

    def wait_idle(self, session_id: str, timeout_s: int = 600,
                  poll: float = 3.0) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            st = self.session_status(session_id)
            if st == "idle":
                return
            if st in ("terminated", "failed", "error"):
                raise KhaError(f"[kha] 会话状态异常: {st}")
            time.sleep(poll)
        raise KhaError(f"[kha] 等待 idle 超时（{timeout_s}s）")

    def collect_text(self, session_id: str) -> str:
        """读协调线程历史事件，拼接全部 agent.message 的文本块。"""
        threads = self._req(
            "GET", f"/v1/sessions/{session_id}/threads").get("items", [])
        coord = next((t for t in threads
                      if not t.get("parent_thread_id")), threads[0] if threads else None)
        if coord is None:
            raise KhaError("[kha] 会话中找不到协调线程")
        events = self._req(
            "GET",
            f"/v1/sessions/{session_id}/threads/{coord['id']}/events"
        ).get("items", [])
        texts: list[str] = []
        for ev in events:
            if ev.get("type") != "agent.message":
                continue
            for blk in (ev.get("data") or {}).get("content") or []:
                if blk.get("type") == "text" and blk.get("text"):
                    texts.append(blk["text"])
        return "\n".join(texts)

    def ask(self, prompt: str, agent_id: str | None = None,
            timeout_s: int = 600) -> str:
        """一站式：确保环境 →（缺省智能体时取列表首个）→ 建会话 → 发消息
        → 等 idle → 取文本。每次评审独立会话，避免跨评审上下文串味。"""
        env_id = self.ensure_environment()
        if agent_id is None:
            agents = self.list_agents()
            if not agents:
                raise KhaError("[kha] 项目中没有任何智能体")
            agent_id = agents[0]["id"]
        sess_id = self.create_session(agent_id, env_id)
        self.send_message(sess_id, prompt)
        self.wait_idle(sess_id, timeout_s=timeout_s)
        return self.collect_text(sess_id)


_client: KhaClient | None = None
_avail_cache: bool | None = None


def _get_kha_client() -> KhaClient:
    global _client
    if _client is None:
        key = os.getenv("MOONSHOT_API_KEY", "")
        if not key:
            raise KhaError("[kha] 未配置 MOONSHOT_API_KEY")
        _client = KhaClient(key)
    return _client


def kha_available() -> bool:
    """真实可用性探测（带缓存）：key 存在且 GET /v1/agents 返回 200。"""
    global _avail_cache
    if _avail_cache is not None:
        return _avail_cache
    if not os.getenv("MOONSHOT_API_KEY"):
        _avail_cache = False
        return False
    try:
        _get_kha_client().list_agents()
        _avail_cache = True
    except Exception:
        _avail_cache = False
    return _avail_cache


def kha_unavailable_reason() -> str:
    if not os.getenv("MOONSHOT_API_KEY"):
        return "未配置 MOONSHOT_API_KEY"
    try:
        _get_kha_client().list_agents()
        return "可用"
    except KhaError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"探测失败: {e}"


class RealKimiHostedAdapter:
    """KHA 通道专家：平台托管 Agent，技能/插件随行（与 hermes 同定位）。

    构造前必须先通过 kha_available() 探测——未开通企业认证时抛 RuntimeError，
    code_review_agent_hub 会据此跳过注册（与其他 adapter 同约定）。
    """

    name = "kimi_hosted"

    def __init__(self) -> None:
        if not kha_available():
            raise RuntimeError(f"kimi-hosted 不可用: {kha_unavailable_reason()}")
        self._client = _get_kha_client()

    def review(self, diff_text: str, context_text: str) -> dict:
        from code_review_compare_mvp import _extract_json_block
        from real_model_adapters import SYSTEM_PROMPT
        reply = self._client.ask(
            SYSTEM_PROMPT.replace("<你的名字>", "kimi_hosted")
            + "\n\n代码上下文：\n" + context_text
            + "\n\nDiff：\n" + diff_text
            + "\n\n直接输出评审 JSON 代码块完成任务，不要用其他工具。",
            timeout_s=600,
        )
        data = _extract_json_block(reply)
        if data is None:
            raise ValueError("kimi_hosted 返回无法解析为 JSON")
        data["agent"] = "kimi_hosted"
        return data
