# -*- coding: utf-8 -*-
"""
Agent Hub 编排层：适配器协议 + webhook 解析 + Compare 编排。

架构定位（来自多 AI 协作平台讨论）：
- 外部成熟 Agent（Kimi / 豆包 / WorkBuddy）是能力底座；
- 本层负责统一入口、并行调度、结果归一、裁决、人工门。
- LangGraph 是可选增强：装了 langgraph 且 LANGGRAPH=1 时走图编排，
  否则走等价的线程池顺序编排，状态契约完全一致。

运行：python code_review_agent_hub.py   （内置冒烟测试）
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from code_review_compare_mvp import (
    FAKE_REVIEWERS,
    SAMPLE_CONTEXT,
    SAMPLE_DIFF,
    Review,
    ReviewParseError,
    normalize_review,
    run_compare,
)

# CLI 直接运行时（非经平台服务）也加载同目录 .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


# ---------------------------------------------------------------- adapter 协议
class ReviewAdapter(Protocol):
    """所有外部 Agent 适配器统一协议。"""

    name: str

    def review(self, diff_text: str, context_text: str) -> dict:
        """返回 Review JSON（dict）。结构不符合 schema 时由 normalize 层兜底/报错。"""
        ...


class FakeAdapter:
    def __init__(self, name: str, fn: Callable[[str, str], dict]):
        self.name = name
        self._fn = fn

    def review(self, diff_text: str, context_text: str) -> dict:
        return self._fn(diff_text, context_text)


class UnavailableAdapter:
    """占位 adapter：真实通道未配置时明确报错，而不是悄悄给假数据。"""

    def __init__(self, name: str, reason: str):
        self.name = name
        self._reason = reason

    def review(self, diff_text: str, context_text: str) -> dict:
        raise RuntimeError(f"[{self.name}] 不可用: {self._reason}")


# ---------------------------------------------------------------- 真实模型适配器（懒加载）
def _load_real_adapters() -> dict[str, ReviewAdapter]:
    """CR_MODE=real 时加载。真实 Kimi/豆包/DeepSeek 走 chat-completions；WorkBuddy 见下。"""
    try:
        from real_model_adapters import (
            RealDeepseekAdapter, RealDoubaoAdapter, RealHermesAdapter,
            RealKimiAdapter, RealQwenAdapter,
        )
    except ImportError as e:
        raise RuntimeError(
            "CR_MODE=real 需要 real_model_adapters.py，且配置 "
            "MOONSHOT_API_KEY / VOLC_ARK_API_KEY / DEEPSEEK_API_KEY"
        ) from e

    adapters: dict[str, ReviewAdapter] = {}
    for name, cls in (("kimi", RealKimiAdapter),
                      ("doubao", RealDoubaoAdapter),
                      ("deepseek", RealDeepseekAdapter),
                      ("qwen", RealQwenAdapter),
                      ("hermes", RealHermesAdapter)):
        try:
            adapters[name] = cls()
        except RuntimeError:
            continue  # 对应 key 未配置则跳过该 Agent
    if not adapters:
        raise RuntimeError("CR_MODE=real 但没有可用模型 key（检查 .env）")
    # WorkBuddy：真实通道需要你按官方 OpenAPI 文档填入 client 凭证；
    # 未配置时保持显式占位，避免误以为评审真的经过了 WorkBuddy。
    if os.getenv("WB_CLIENT_ID") and os.getenv("WB_CLIENT_SECRET"):
        try:
            from real_model_adapters import RealWorkBuddyAdapter
            adapters["workbuddy"] = RealWorkBuddyAdapter()
        except Exception:
            adapters["workbuddy"] = UnavailableAdapter(
                "workbuddy", "WB 凭证已配置但适配器初始化失败，请检查 real_model_adapters.RealWorkBuddyAdapter")
    else:
        adapters["workbuddy"] = UnavailableAdapter(
            "workbuddy", "未配置 WB_CLIENT_ID/WB_CLIENT_SECRET（本地助理通道）")
    return adapters


def get_adapters(mode: str | None = None) -> dict[str, ReviewAdapter]:
    mode = (mode or os.getenv("CR_MODE", "fake")).lower()
    if mode == "real":
        return _load_real_adapters()
    return {name: FakeAdapter(name, fn) for name, fn in FAKE_REVIEWERS.items()}


# ---------------------------------------------------------------- webhook 解析
@dataclass
class CodeReviewEvent:
    provider: str                      # gitlab | github | manual
    repo: str
    mr_id: str
    title: str = ""
    url: str = ""
    author: str = ""
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "repo": self.repo, "mr_id": self.mr_id,
            "title": self.title, "url": self.url, "author": self.author,
        }


def parse_gitlab_mr(payload: dict) -> CodeReviewEvent:
    attrs = payload.get("object_attributes", {})
    project = payload.get("project", {})
    return CodeReviewEvent(
        provider="gitlab",
        repo=project.get("path_with_namespace", "unknown/unknown"),
        mr_id=str(attrs.get("iid") or attrs.get("id") or ""),
        title=attrs.get("title", ""),
        url=attrs.get("url", ""),
        author=(payload.get("user") or {}).get("username", ""),
        raw=payload,
    )


def parse_github_pr(payload: dict) -> CodeReviewEvent:
    pr = payload.get("pull_request", {})
    repo = (payload.get("repository") or {}).get("full_name", "unknown/unknown")
    return CodeReviewEvent(
        provider="github",
        repo=repo,
        mr_id=str(pr.get("number", "")),
        title=pr.get("title", ""),
        url=pr.get("html_url", ""),
        author=(pr.get("user") or {}).get("login", ""),
        raw=payload,
    )


# ---------------------------------------------------------------- diff 获取
def fetch_diff(event: CodeReviewEvent) -> tuple[str, str]:
    """返回 (diff_text, context_text)。

    ALLOW_STUB_DIFF=1（默认）时用内置样例 diff，便于开箱演示；
    生产环境置 0 并配置 GITLAB_TOKEN / GITHUB_TOKEN，按 provider 拉真实 diff。
    """
    allow_stub = os.getenv("ALLOW_STUB_DIFF", "1") == "1"
    if allow_stub or event.provider == "manual":
        return SAMPLE_DIFF, SAMPLE_CONTEXT

    import requests
    if event.provider == "gitlab":
        token = os.environ["GITLAB_API_TOKEN"]
        api = os.getenv("GITLAB_API_URL", "https://gitlab.com/api/v4")
        iid = (event.raw.get("object_attributes") or {}).get("iid") or event.mr_id
        from urllib.parse import quote
        url = f"{api}/projects/{quote(event.repo, safe='')}/merge_requests/{iid}/diffs"
        r = requests.get(url, headers={"PRIVATE-TOKEN": token}, timeout=30)
        r.raise_for_status()
        diff_text = "\n".join(d.get("diff", "") for d in r.json())
        return diff_text, f"仓库: {event.repo}\nMR: {event.mr_id} {event.title}\n"

    if event.provider == "github":
        token = os.environ["GITHUB_API_TOKEN"]
        api = os.getenv("GITHUB_API_URL", "https://api.github.com")
        url = f"{api}/repos/{event.repo}/pulls/{event.mr_id}"
        r = requests.get(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
        }, timeout=30)
        r.raise_for_status()
        return r.text, f"仓库: {event.repo}\nPR: #{event.mr_id} {event.title}\n"

    raise ValueError(f"未知 provider: {event.provider}")


# ---------------------------------------------------------------- 编排
def run_compare_hub(
    diff_text: str,
    context_text: str,
    adapters: dict[str, ReviewAdapter] | None = None,
    event: CodeReviewEvent | None = None,
) -> dict:
    """并行调用各 Agent → 归一化 → 裁决 → 产出待人工确认的状态。"""
    adapters = adapters or get_adapters()
    run_id = f"cr_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    event = event or CodeReviewEvent(provider="manual", repo="local/sample", mr_id="0")

    raw_reviews: dict[str, Any] = {}
    errors: dict[str, str] = {}
    reviews: list[Review] = []

    def _call(name: str, adapter: ReviewAdapter) -> tuple[str, Any]:
        return name, adapter.review(diff_text, context_text)

    with ThreadPoolExecutor(max_workers=max(1, len(adapters))) as pool:
        futures = {pool.submit(_call, n, a): n for n, a in adapters.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                _, raw = fut.result()
                raw_reviews[name] = raw
            except Exception as e:  # 单个 Agent 挂掉不拖垮整个 Compare
                errors[name] = f"{type(e).__name__}: {e}"
                raw_reviews[name] = None

    for name, raw in raw_reviews.items():
        if raw is None:
            continue
        try:
            reviews.append(normalize_review(name, raw))
        except ReviewParseError as e:
            errors[name] = str(e)

    if not reviews:
        raise RuntimeError(f"所有 Agent 评审失败: {errors}")

    result = run_compare(reviews)
    state = {
        "run_id": run_id,
        "task_type": "code_review_compare",
        "event": event.to_dict(),
        "agent_errors": errors,
        **result,
        "human_decision": None,
        "post_status": None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return state


# ---------------------------------------------------------------- 人工决定（平台与 CLI 共用）
def apply_human_decision(state: dict, action: str, comment: str, actor: str) -> dict:
    """action: approve | comment | request_changes。只改状态，不外发。"""
    if not state.get("pending_human"):
        raise RuntimeError("该 run 不处于待人工确认状态")
    if action not in ("approve", "comment", "request_changes"):
        raise ValueError(f"非法 action: {action}")
    state["human_decision"] = {
        "action": action, "comment": comment, "actor": actor,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state["pending_human"] = False
    return state


# ---------------------------------------------------------------- 冒烟
def _smoke() -> None:
    event = CodeReviewEvent(provider="manual", repo="acme/payment", mr_id="123")
    state = run_compare_hub(SAMPLE_DIFF, SAMPLE_CONTEXT,
                            adapters=get_adapters("fake"), event=event)
    assert state["verdict"]["recommendation"] == "request_changes"
    assert state["pending_human"] is True
    assert not state["agent_errors"], state["agent_errors"]

    apply_human_decision(state, "request_changes", "请补唯一约束和并发单测后再合并", "smoke")
    assert state["pending_human"] is False

    # webhook 解析
    gl = parse_gitlab_mr({"object_attributes": {"iid": 7, "title": "t", "url": "u"},
                          "project": {"path_with_namespace": "a/b"},
                          "user": {"username": "x"}})
    assert gl.provider == "gitlab" and gl.repo == "a/b" and gl.mr_id == "7"
    gh = parse_github_pr({"pull_request": {"number": 9, "title": "t"},
                          "repository": {"full_name": "c/d"}})
    assert gh.provider == "github" and gh.repo == "c/d" and gh.mr_id == "9"
    print("hub smoke OK")


if __name__ == "__main__":
    _smoke()
