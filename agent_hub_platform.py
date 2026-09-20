# -*- coding: utf-8 -*-
"""
Agent Hub 平台服务：FastAPI + SQLite 持久化/审计 + GitLab/GitHub webhook + Web 控制台。

安全默认值（开箱不越权）：
- CR_MODE=fake        用内置三家假 Agent 演示闭环
- ALLOW_STUB_DIFF=1   未接 GitLab/GitHub token 前用样例 diff
- DRY_RUN_POST=1      人工确认后只记录、不外发真实评论
- WEBHOOK_SECRET 未设置时不校验来源（仅本地开发）

启动：uvicorn agent_hub_platform:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Request

# 启动时加载同目录 .env（密钥只留在本机，不进代码库）
load_dotenv(Path(__file__).resolve().parent / ".env")
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from code_review_agent_hub import (
    CodeReviewEvent,
    apply_human_decision,
    fetch_diff,
    get_adapters,
    parse_github_pr,
    parse_gitlab_mr,
    run_compare_hub,
)
from proposal_collab import (
    cross_review,
    iterate_plans,
    new_proposal_state,
    write_final_plan,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("AGENT_HUB_DB", str(BASE_DIR / "agent_hub.db"))
UI_PATH = BASE_DIR / "agent_hub_platform_ui.html"
DRY_RUN_POST = os.getenv("DRY_RUN_POST", "1") == "1"

app = FastAPI(title="Agent Hub", version="0.1.0")
_db_lock = threading.Lock()


# ---------------------------------------------------------------- store
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _db_lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            provider TEXT, repo TEXT, mr_id TEXT, title TEXT,
            status TEXT, state_json TEXT,
            created_at TEXT, updated_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT, actor TEXT, action TEXT, comment TEXT,
            post_status TEXT, created_at TEXT)""")


def save_run(state: dict) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    event = state.get("event", {})
    status = state.get("status") or (
        "pending_human" if state.get("pending_human") else "resolved")
    with _db_lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
            (state["run_id"], event.get("provider", "manual"),
             event.get("repo", ""), event.get("mr_id", ""),
             event.get("title", ""),
             status,
             json.dumps(state, ensure_ascii=False), now, now),
        )


def get_run(run_id: str) -> dict | None:
    with _db_lock, _conn() as c:
        row = c.execute("SELECT state_json FROM runs WHERE run_id=?",
                        (run_id,)).fetchone()
    return json.loads(row["state_json"]) if row else None


def list_runs(limit: int = 50) -> list[dict]:
    with _db_lock, _conn() as c:
        rows = c.execute(
            "SELECT run_id, provider, repo, mr_id, title, status, created_at "
            "FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def audit(run_id: str, actor: str, action: str, comment: str,
          post_status: str) -> None:
    with _db_lock, _conn() as c:
        c.execute("INSERT INTO audit VALUES (NULL,?,?,?,?,?,?)",
                  (run_id, actor, action, comment, post_status,
                   time.strftime("%Y-%m-%d %H:%M:%S")))


def get_audit(run_id: str) -> list[dict]:
    with _db_lock, _conn() as c:
        rows = c.execute(
            "SELECT actor, action, comment, post_status, created_at FROM audit "
            "WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    return [dict(r) for r in rows]


init_db()


# ---------------------------------------------------------------- webhook 校验
def _verify(request: Request, body: bytes) -> None:
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        return  # 本地开发默认不校验
    if "x-gitlab-token" in request.headers:
        token = request.headers["x-gitlab-token"]
        if not hmac.compare_digest(token, secret):
            raise HTTPException(401, "GitLab webhook token 校验失败")
    elif "x-hub-signature-256" in request.headers:
        sig = request.headers["x-hub-signature-256"]
        expected = "sha256=" + hmac.new(secret.encode(), body,
                                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(401, "GitHub webhook 签名校验失败")
    else:
        raise HTTPException(401, "缺少 webhook 校验头")


# ---------------------------------------------------------------- 外发（默认 dry-run）
def post_comment(state: dict, action: str, comment: str) -> str:
    """人工确认后的外发。DRY_RUN_POST=1 时只返回将要执行的动作。"""
    if DRY_RUN_POST:
        return f"dry_run: 将对 {state['event']['provider']} 执行 {action}（未真实外发）"
    # TODO: 真实外发 —— GitLab: POST /projects/:id/merge_requests/:iid/notes
    #       GitHub: POST /repos/:repo/pulls/:number/reviews
    # 需要 GITLAB_API_TOKEN / GITHUB_API_TOKEN 与真实 diff 获取一起开启。
    return "not_configured: 生产外发需置 DRY_RUN_POST=0 并配置平台 token"


# ---------------------------------------------------------------- API
class CompareIn(BaseModel):
    title: str | None = None
    diff: str | None = None
    context: str | None = None


class DecisionIn(BaseModel):
    action: str                      # approve | comment | request_changes
    comment: str = ""
    actor: str = "web"


def _execute(event: CodeReviewEvent, diff: str | None, context: str | None) -> dict:
    diff_text, context_text = (diff, context) if diff is not None else fetch_diff(event)
    state = run_compare_hub(diff_text, context_text,
                            adapters=get_adapters(), event=event)
    save_run(state)
    return state


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": os.getenv("CR_MODE", "fake"),
            "dry_run_post": DRY_RUN_POST, "time": time.strftime("%Y-%m-%d %H:%M:%S")}


@app.post("/events/gitlab")
async def gitlab_event(request: Request) -> dict:
    body = await request.body()
    _verify(request, body)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "payload 不是合法 JSON")
    state = _execute(parse_gitlab_mr(payload), None, None)
    return {"run_id": state["run_id"],
            "status": "pending_human" if state["pending_human"] else "resolved",
            "verdict": state["verdict"]}


@app.post("/events/github")
async def github_event(request: Request) -> dict:
    body = await request.body()
    _verify(request, body)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "payload 不是合法 JSON")
    state = _execute(parse_github_pr(payload), None, None)
    return {"run_id": state["run_id"],
            "status": "pending_human" if state["pending_human"] else "resolved",
            "verdict": state["verdict"]}


@app.post("/compare/run")
def compare_run(body: CompareIn) -> dict:
    """手动触发一次 Compare（默认用内置样例 diff，便于演示）。"""
    from code_review_compare_mvp import SAMPLE_CONTEXT, SAMPLE_DIFF
    event = CodeReviewEvent(provider="manual", repo="local/manual", mr_id="0",
                            title=(body.title or "手动提交的 diff 评审").strip())
    state = _execute(event, body.diff or SAMPLE_DIFF, body.context or SAMPLE_CONTEXT)
    return {"run_id": state["run_id"],
            "status": "pending_human" if state["pending_human"] else "resolved",
            "verdict": state["verdict"]}


@app.get("/runs")
def runs() -> dict:
    return {"runs": [r for r in list_runs()
                     if not r["run_id"].startswith("pp_")]}


@app.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    state = get_run(run_id)
    if not state:
        raise HTTPException(404, "run 不存在")
    state["audit"] = get_audit(run_id)
    return state


@app.post("/runs/{run_id}/human-decision")
def human_decision(run_id: str, body: DecisionIn) -> dict:
    state = get_run(run_id)
    if not state:
        raise HTTPException(404, "run 不存在")
    try:
        apply_human_decision(state, body.action, body.comment, body.actor)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    post_status = post_comment(state, body.action, body.comment)
    state["post_status"] = post_status
    save_run(state)
    audit(run_id, body.actor, body.action, body.comment, post_status)
    return {"ok": True, "run_id": run_id, "action": body.action,
            "post_status": post_status}


@app.get("/ui", response_class=HTMLResponse)
def ui() -> HTMLResponse:
    if not UI_PATH.exists():
        raise HTTPException(404, "UI 文件缺失: agent_hub_platform_ui.html")
    return HTMLResponse(UI_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 方案共创
class ProposalIn(BaseModel):
    idea: str
    context: str | None = None


class DecideIn(BaseModel):
    selected: str                # 方案 key：kimi | minimax | deepseek
    comment: str = ""            # 补充意见（交给产出 AI 写入定稿）
    actor: str = "web"


class IterateIn(BaseModel):
    instruction: str = ""        # 本轮迭代要求
    actor: str = "web"


def _get_proposal(proposal_id: str) -> dict:
    state = get_run(proposal_id)
    if not state or state.get("mode") != "proposal":
        raise HTTPException(404, "方案不存在")
    return state


@app.post("/proposal/run")
def proposal_run(body: ProposalIn) -> dict:
    """方案共创：出题 → 三家出方案 → 交叉评审（阻塞，真实模型 2–5 分钟）。"""
    idea = (body.idea or "").strip()
    if not idea:
        raise HTTPException(400, "想法/需求不能为空")
    proposal_id = "pp_" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    state = new_proposal_state(idea, body.context or "", proposal_id)
    save_run(state)
    return {"proposal_id": proposal_id, "phase": state["phase"],
            "plans": list(state["plans"].keys()),
            "model_errors": state["model_errors"]}


@app.get("/proposals")
def proposals() -> dict:
    rows = [r for r in list_runs() if r["run_id"].startswith("pp_")]
    return {"proposals": rows}


@app.get("/proposal/{proposal_id}")
def proposal_detail(proposal_id: str) -> dict:
    state = _get_proposal(proposal_id)
    state["audit"] = get_audit(proposal_id)
    return state


@app.post("/proposal/{proposal_id}/decide")
def proposal_decide(proposal_id: str, body: DecideIn) -> dict:
    """人工拍板：选定方案，由产出该方案的 AI 编写定稿。"""
    state = _get_proposal(proposal_id)
    if state.get("phase") not in ("awaiting_decision", "final"):
        raise HTTPException(400, f"当前阶段 {state.get('phase')} 不能拍板")
    if body.selected not in state.get("plans", {}):
        raise HTTPException(400, f"方案 {body.selected} 不存在")
    plan = state["plans"][body.selected]
    try:
        final_plan = write_final_plan(body.selected, state["idea"],
                                      state.get("context", ""), plan,
                                      body.comment)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"定稿编写失败: {e}")
    state["decision"] = {
        "selected": body.selected, "comment": body.comment, "actor": body.actor,
        "final_plan": final_plan,
        "decided_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state["phase"] = "final"
    state["status"] = "final"
    save_run(state)
    audit(proposal_id, body.actor, f"decide:{body.selected}",
          body.comment, "final_plan_written")
    return {"ok": True, "proposal_id": proposal_id,
            "selected": body.selected, "final_plan_length": len(final_plan)}


@app.post("/proposal/{proposal_id}/iterate")
def proposal_iterate(proposal_id: str, body: IterateIn) -> dict:
    """迭代轮：三家基于当前定稿各自优化，回到待拍板状态。"""
    state = _get_proposal(proposal_id)
    chosen = (state.get("decision") or {}).get("final_plan")
    if not chosen:
        raise HTTPException(400, "请先拍板选定方案，再发起迭代")
    prev = {"round": state.get("round", 1),
            "instruction": (state.get("decision") or {}).get("comment", ""),
            "plans": state.get("plans", {}),
            "reviews": state.get("reviews", {}),
            "model_errors": state.get("model_errors", {})}
    state.setdefault("iterations", []).append(prev)
    t0 = time.time()
    it = iterate_plans(state["idea"], state.get("context", ""), chosen,
                       body.instruction, state.get("reviews", {}))
    state["plans"], state["reviews"] = it["results"], {}
    state["model_errors"] = it["errors"]
    if len(state["plans"]) >= 2:
        rev = cross_review(state["idea"], state.get("context", ""),
                           state["plans"])
        state["reviews"] = rev["results"]
        state["model_errors"].update(rev["errors"])
    state["round"] = state.get("round", 1) + 1
    state["decision"] = None
    state["phase"] = "awaiting_decision"
    state["status"] = "awaiting_decision"
    state["elapsed_s"] = round(time.time() - t0, 1)
    save_run(state)
    audit(proposal_id, body.actor, "iterate",
          body.instruction, f"round {state['round']}")
    return {"ok": True, "proposal_id": proposal_id, "round": state["round"],
            "plans": list(state["plans"].keys()),
            "model_errors": state["model_errors"]}


# ---------------------------------------------------------------- MCP 端点
class TokenAuthMiddleware:
    """MCP 端点的 Bearer Token 校验。

    AGENT_HUB_API_KEY 设置后生效；未设置则不校验（仅本地开发）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            expected = os.getenv("AGENT_HUB_API_KEY", "")
            if expected:
                headers = {k.decode().lower(): v.decode()
                           for k, v in scope.get("headers", [])}
                if headers.get("authorization") != f"Bearer {expected}":
                    resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


from agent_hub_mcp import mcp as _mcp  # noqa: E402

app.mount("/mcp", TokenAuthMiddleware(_mcp.streamable_http_app()))


# app.mount 不会执行子应用的 lifespan；MCP session manager 的任务组
# 需要在主应用启动时手动进入，否则 /mcp 请求报 "Task group is not initialized"。
_mcp_cm = None


@app.on_event("startup")
async def _start_mcp_session_manager() -> None:
    global _mcp_cm
    _mcp_cm = _mcp.session_manager.run()
    await _mcp_cm.__aenter__()


@app.on_event("shutdown")
async def _stop_mcp_session_manager() -> None:
    global _mcp_cm
    if _mcp_cm is not None:
        await _mcp_cm.__aexit__(None, None, None)
        _mcp_cm = None
