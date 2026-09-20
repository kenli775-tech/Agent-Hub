# -*- coding: utf-8 -*-
"""
真实模型适配器：Kimi（Moonshot）、豆包（火山方舟）、DeepSeek、WorkBuddy。
所有适配器把输出强约束到统一的 Review JSON schema，
并复用 code_review_compare_mvp._extract_json_block 做容错解析。

配置（环境变量）：
- MOONSHOT_API_KEY / MOONSHOT_MODEL    Kimi（默认 kimi-k2.7-code）
- VOLC_ARK_API_KEY / VOLC_ARK_MODEL    豆包火山方舟（默认 doubao-seed-2-1-pro-260915）
- DEEPSEEK_API_KEY / DEEPSEEK_MODEL    DeepSeek（默认 deepseek-chat）
- WB_CLIENT_ID / WB_CLIENT_SECRET      WorkBuddy 开放平台应用凭证（OAuth 2.0 + PKCE）

WorkBuddy 首次使用需人工授权：运行 python workbuddy_auth.py 按提示完成。

安全约定：
- 真实代码 diff 属 internal 级数据；四家均走官方 API 通道。
- WorkBuddy 评审走云端助理任务通道，不申请本机能力 scope。
- 任何 key / token 不打印、不进日志；.wb_tokens.json 等凭证文件勿提交 Git。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from code_review_compare_mvp import _extract_json_block

SYSTEM_PROMPT = """你是代码评审 Agent。只输出一个 JSON 代码块，不要输出任何其他文字。
JSON schema：
{
  "agent": "<你的名字>",
  "summary": "一句话总结",
  "findings": [
    {
      "id": "<agent>_001",
      "severity": "blocker|major|minor|nit",
      "file": "文件路径",
      "line_hint": 行号或 null,
      "category": "correctness|concurrency|security|performance|test|style",
      "message": "问题描述，必须具体",
      "suggestion": "修改建议",
      "confidence": 0.0-1.0,
      "evidence": ["代码位置或依据"],
      "needs_human": false
    }
  ],
  "missing_context": ["评审所需但缺失的信息"],
  "approved": false
}
规则：
- 禁止泛泛而谈；每个 finding 必须有 file/evidence/suggestion。
- 不确定就降低 confidence 并置 needs_human=true。
- 不批准合并，只给评审结论。
- diff 不足时输出 missing_context，不要编造。"""


def _chat_completion(base_url: str, api_key: str, model: str,
                     messages: list[dict], timeout: int | None = None) -> str:
    payload: dict[str, Any] = {"model": model, "messages": messages}
    # 部分模型（如 kimi-k2.x）只允许默认 temperature，除非显式配置否则不传
    temp = os.getenv("MODEL_TEMPERATURE")
    if temp:
        payload["temperature"] = float(temp)
    if timeout is None:
        timeout = int(os.getenv("MODEL_TIMEOUT", "240"))  # 推理模型首包较慢
    r = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


class RealKimiAdapter:
    name = "kimi"

    def __init__(self) -> None:
        self._key = os.environ.get("MOONSHOT_API_KEY", "")
        self._model = os.getenv("MOONSHOT_MODEL", "kimi-k2-0711-preview")
        self._base = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
        if not self._key:
            raise RuntimeError("缺少 MOONSHOT_API_KEY")

    def review(self, diff_text: str, context_text: str) -> dict:
        text = _chat_completion(self._base, self._key, self._model, [
            {"role": "system", "content": SYSTEM_PROMPT.replace("<你的名字>", "kimi")},
            {"role": "user", "content": f"代码上下文：\n{context_text}\n\nDiff：\n{diff_text}"},
        ])
        data = _extract_json_block(text)
        if data is None:
            raise ValueError("Kimi 返回无法解析为 JSON")
        data["agent"] = "kimi"
        return data


class RealDoubaoAdapter:
    name = "doubao"

    def __init__(self) -> None:
        self._key = os.environ.get("VOLC_ARK_API_KEY", "")
        self._model = os.getenv("VOLC_ARK_MODEL", "doubao-seed-1-6-250615")
        self._base = os.getenv("VOLC_ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        if not self._key:
            raise RuntimeError("缺少 VOLC_ARK_API_KEY")

    def review(self, diff_text: str, context_text: str) -> dict:
        text = _chat_completion(self._base, self._key, self._model, [
            {"role": "system", "content": SYSTEM_PROMPT.replace("<你的名字>", "doubao")},
            {"role": "user", "content": f"代码上下文：\n{context_text}\n\nDiff：\n{diff_text}"},
        ])
        data = _extract_json_block(text)
        if data is None:
            raise ValueError("豆包返回无法解析为 JSON")
        data["agent"] = "doubao"
        return data


class RealQwenAdapter:
    """千问（阿里 DashScope，OpenAI 兼容模式）。"""
    name = "qwen"

    def __init__(self) -> None:
        self._key = os.environ.get("QWEN_API_KEY", "")
        self._model = os.getenv("QWEN_MODEL", "qwen-plus")
        self._base = os.getenv("QWEN_BASE_URL",
                               "https://dashscope.aliyuncs.com/compatible-mode/v1")
        if not self._key:
            raise RuntimeError("缺少 QWEN_API_KEY")

    def review(self, diff_text: str, context_text: str) -> dict:
        text = _chat_completion(self._base, self._key, self._model, [
            {"role": "system", "content": SYSTEM_PROMPT.replace("<你的名字>", "qwen")},
            {"role": "user", "content": f"代码上下文：\n{context_text}\n\nDiff：\n{diff_text}"},
        ])
        data = _extract_json_block(text)
        if data is None:
            raise ValueError("千问返回无法解析为 JSON")
        data["agent"] = "qwen"
        return data


class RealDeepseekAdapter:
    name = "deepseek"

    def __init__(self) -> None:
        self._key = os.environ.get("DEEPSEEK_API_KEY", "")
        self._model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self._base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        if not self._key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY")

    def review(self, diff_text: str, context_text: str) -> dict:
        text = _chat_completion(self._base, self._key, self._model, [
            {"role": "system", "content": SYSTEM_PROMPT.replace("<你的名字>", "deepseek")},
            {"role": "user", "content": f"代码上下文：\n{context_text}\n\nDiff：\n{diff_text}"},
        ])
        data = _extract_json_block(text)
        if data is None:
            raise ValueError("DeepSeek 返回无法解析为 JSON")
        data["agent"] = "deepseek"
        return data


class NeedManualAuth(RuntimeError):
    """首次使用 WorkBuddy 时需要人工完成 OAuth 授权。

    authorize_url 交给用户在浏览器打开，授权后运行
    python workbuddy_auth.py 完成自动换票（凭证与官方冒烟脚本共用
    .wb_token.json），之后平台自动刷新。
    """

    def __init__(self, authorize_url: str):
        super().__init__(
            "WorkBuddy 需要人工授权：浏览器打开以下链接完成授权（PC 端 WorkBuddy 需已登录），"
            "然后运行 python workbuddy_auth.py。\n" + authorize_url
        )
        self.authorize_url = authorize_url


class WorkBuddyOffline(RuntimeError):
    """PC 端 WorkBuddy 客户端不在线。评审不中断，记录后由其他 Agent 出结论。"""


class RealWorkBuddyAdapter:
    """WorkBuddy 真实适配器 —— 官方「本地助理」通道。

    依据《WorkBuddy接入形态-查证结论v1.0》与官方冒烟脚本 wb_openapi_smoke.py：
    - Base URL: https://www.workbuddy.cn/openapi/v2（注意不是 open.workbuddy.cn）
    - OAuth 2.1: GET {BASE}/authorize → POST {BASE}/token（form-urlencoded）
    - Scope 只申请两个：user.localassistant.invokable / user.localassistant.readable
    - access_token 有效期按 3600s 设计（文档 24h 与 3600 冲突，取保守值），余量 300s
    - 评审链路：查在线 → POST /localassistant/message 下发评审指令
      → 增量轮询 GET /localassistant/message?message_id=... 收取回复 → 解析 JSON

    敏感边界：数据只到用户 PC 端本机 Agent，不走云端任务/ACP 沙箱。
    凭证缓存文件 .wb_token.json 与官方冒烟脚本共用，格式一致。
    """

    name = "workbuddy"
    BASE = "https://www.workbuddy.cn/openapi/v2"
    SCOPES = "user.localassistant.invokable user.localassistant.readable"
    TOKEN_PATH = Path(__file__).resolve().parent / ".wb_token.json"
    SAFETY_MARGIN = 300  # 秒；提前刷新余量

    def __init__(self) -> None:
        self._client_id = os.environ.get("WB_CLIENT_ID", "")
        self._client_secret = os.environ.get("WB_CLIENT_SECRET", "")
        self._redirect_uri = os.getenv("WB_REDIRECT_URI", "http://127.0.0.1:8765/callback")
        if not (self._client_id and self._client_secret):
            raise RuntimeError("缺少 WB_CLIENT_ID / WB_CLIENT_SECRET")

    # ---- OAuth ----
    def _load_tokens(self) -> dict | None:
        if not self.TOKEN_PATH.exists():
            return None
        try:
            return json.loads(self.TOKEN_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_tokens(self, tokens: dict) -> None:
        self.TOKEN_PATH.write_text(
            json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")

    def authorize_url(self) -> str:
        import secrets
        state = secrets.token_urlsafe(16)
        query = requests.compat.urlencode({
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": self.SCOPES,
            "state": state,
        })
        return f"{self.BASE}/authorize?{query}"

    def exchange_code(self, code: str) -> dict:
        """用授权码换票据（授权码一次性，10 分钟有效）。"""
        r = requests.post(f"{self.BASE}/token",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={
                              "grant_type": "authorization_code",
                              "code": code,
                              "client_id": self._client_id,
                              "client_secret": self._client_secret,
                              "redirect_uri": self._redirect_uri,
                          }, timeout=30)
        r.raise_for_status()
        body = r.json()
        tokens = {
            "access_token": body.get("access_token"),
            "refresh_token": body.get("refresh_token"),
            "expires_at": time.time() + int(body.get("expires_in") or 3600),
            "scope": body.get("scope"),
            "open_id": body.get("open_id"),
        }
        self._save_tokens(tokens)
        return tokens

    def _refresh(self, tokens: dict) -> dict:
        r = requests.post(f"{self.BASE}/token",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={
                              "grant_type": "refresh_token",
                              "refresh_token": tokens["refresh_token"],
                              "client_id": self._client_id,
                              "client_secret": self._client_secret,
                          }, timeout=30)
        r.raise_for_status()
        body = r.json()
        new_tokens = {
            "access_token": body.get("access_token"),
            # refresh_token 可能不轮换，保留旧的
            "refresh_token": body.get("refresh_token") or tokens["refresh_token"],
            "expires_at": time.time() + int(body.get("expires_in") or 3600),
            "scope": body.get("scope", tokens.get("scope")),
            "open_id": body.get("open_id", tokens.get("open_id")),
        }
        self._save_tokens(new_tokens)
        return new_tokens

    def _ensure_token(self) -> str:
        tokens = self._load_tokens()
        if not tokens or not tokens.get("access_token"):
            raise NeedManualAuth(self.authorize_url())
        if float(tokens.get("expires_at") or 0) - self.SAFETY_MARGIN > time.time():
            return tokens["access_token"]
        if tokens.get("refresh_token"):
            return self._refresh(tokens)["access_token"]
        raise NeedManualAuth(self.authorize_url())

    # ---- 本地助理通道 ----
    def _api(self, method: str, path: str, token: str, **kw) -> dict:
        r = requests.request(method, f"{self.BASE}{path}",
                             headers={"Authorization": f"Bearer {token}",
                                      "Accept": "application/json"},
                             timeout=kw.pop("timeout", 30), **kw)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _content_to_text(content) -> str:
        """消息 content 为数组（文本块/富文本块），拼成纯文本。"""
        if isinstance(content, str):
            return content
        parts = []
        for block in (content or []):
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "\n".join(p for p in parts if p)

    def review(self, diff_text: str, context_text: str) -> dict:
        token = self._ensure_token()

        status = self._api("GET", "/localassistant", token)
        if not (status.get("data") or {}).get("online"):
            raise WorkBuddyOffline(
                "PC 端 WorkBuddy 不在线（GET /localassistant → online=false）；"
                "请启动并登录 PC 客户端后重试")

        prompt = (SYSTEM_PROMPT.replace("<你的名字>", "workbuddy")
                  + f"\n\n代码上下文：\n{context_text}\n\nDiff：\n{diff_text}")
        sent = self._api("POST", "/localassistant/message", token,
                         headers={"Content-Type": "application/json"},
                         data=json.dumps({"content": prompt, "msg_type": "text"}))
        sent_id = (sent.get("data") or {}).get("message_id")
        if not sent_id:
            raise ValueError(f"发送消息未返回 message_id: {sent}")

        # 增量轮询：只收取 sent_id 之后的消息，等助理文本回复
        timeout_s = int(os.getenv("MODEL_TIMEOUT", "240"))
        deadline = time.time() + timeout_s
        delay = 2.0
        while time.time() < deadline:
            hist = self._api("GET", f"/localassistant/message?message_id={sent_id}",
                             token, timeout=30)
            messages = (hist.get("data") or {}).get("messages") or []
            for m in messages:
                if m.get("msg_type") == "permission_response":
                    raise RuntimeError(
                        "WorkBuddy 助理发起权限确认（permission_response），"
                        "请在 PC 端人工确认后重试；AI 不代替人工终审")
                if m.get("role") in ("assistant", "workbuddy", "ai") or \
                   (m.get("message_id") != sent_id and m.get("role") != "user"):
                    text = self._content_to_text(m.get("content"))
                    data = _extract_json_block(text)
                    if data is not None:
                        data["agent"] = "workbuddy"
                        return data
            time.sleep(delay)
            delay = min(delay * 2, 15)  # 简单指数退避，兼顾 429 建议
        raise TimeoutError(f"等待 WorkBuddy 回复超时（{timeout_s}s），请确认 PC 端助理已处理该消息")


# ---------------------------------------------------------------- hermes（ACP 直通）
_HERMES_CLIENT = None


def _get_hermes_client():
    """进程内复用一条 ACP 连接（hermes 子进程启动约 1-2s，不每轮重开）。"""
    global _HERMES_CLIENT
    if _HERMES_CLIENT is None:
        from acp_channel import HermesAcpClient
        _HERMES_CLIENT = HermesAcpClient()
    return _HERMES_CLIENT


class RealHermesAdapter:
    """hermes-agent（本机 ACP 通道）：对方是完整 Agent，自带技能库随行。

    与裸模型 adapter 的本质区别：hermes 在回答前可自主调用它的技能
    （pdf/docx/xlsx/notion/arxiv...），适合做"带技能的专家"角色。
    """
    name = "hermes"

    def __init__(self) -> None:
        from acp_channel import hermes_available
        if not hermes_available():
            raise RuntimeError("hermes-agent ACP 通道不可用（未安装 hermes）")
        self._client = _get_hermes_client()

    def review(self, diff_text: str, context_text: str) -> dict:
        reply = self._client.ask(
            SYSTEM_PROMPT.replace("<你的名字>", "hermes")
            + "\n\n代码上下文：\n" + context_text
            + "\n\nDiff：\n" + diff_text
            + "\n\n直接输出评审 JSON 代码块完成任务，不要用其他工具。"
        )
        data = _extract_json_block(reply)
        if data is None:
            raise ValueError("hermes 返回无法解析为 JSON")
        data["agent"] = "hermes"
        return data
