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
            created_at TEXT, updated_at TEXT, diff_sha1 TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT, actor TEXT, action TEXT, comment TEXT,
            post_status TEXT, created_at TEXT)""")
        # 幂等键（2026-09-20）：同一份 diff 被重复提交时去重。对已存在的旧库做防御性加列。
        try:
            c.execute("ALTER TABLE runs ADD COLUMN idem_key TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在
        c.execute("CREATE INDEX IF NOT EXISTS idx_runs_idem ON runs(idem_key)")
        # 被审输入指纹（2026-09-20 P0-2）：与幂等键同为内容哈希，便于 SQL 直接查
        # "哪几次 run 评的是同一份 diff"；对已存在的旧库做防御性加列。
        # 注意：CREATE TABLE IF NOT EXISTS 对既有表不生效，旧库**只能**靠这条 ALTER 补列，
        # 缺了它 save_run 会因 no such column 直接失败。
        try:
            c.execute("ALTER TABLE runs ADD COLUMN diff_sha1 TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在


def find_run_by_idem(idem_key: str) -> dict | None:
    """按幂等键找最近一次在库的 run（返回摘要，不含全量 state）。"""
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT run_id, status FROM runs WHERE idem_key=? "
            "ORDER BY created_at DESC LIMIT 1", (idem_key,)).fetchone()
    return dict(row) if row else None


def save_run(state: dict) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    event = state.get("event", {})
    status = state.get("status") or (
        "pending_human" if state.get("pending_human") else "resolved")
    with _db_lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id, provider, repo, mr_id, title, status, state_json, "
            " created_at, updated_at, idem_key, diff_sha1) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (state["run_id"], event.get("provider", "manual"),
             event.get("repo", ""), event.get("mr_id", ""),
             event.get("title", ""),
             status,
             json.dumps(state, ensure_ascii=False), now, now,
             state.get("idempotency_key"),
             (state.get("inputs") or {}).get("diff_sha1")),
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
    # 幂等键（2026-09-20）：客户端对同一份 diff+context 生成内容哈希，
    # 重试/重复提交时服务端去重，返回首个 run，避免同 diff 被会诊多遍。
    idempotency_key: str | None = None


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


def _channel_status() -> dict:
    """各 Agent 直通通道的就绪状态（2026-09-20 P1-5）。

    此前【专家会诊】提示一律写"hermes/WorkBuddy/KHA 直通通道在线"，但 WorkBuddy
    在每个 run 都报未配置凭证、KHA 403 —— 模型据此向用户承诺了不存在的能力。

    只做**廉价**判断（文件存在性 / 环境变量），**不做网络探测**：/health 是高频
    探测端点（调用方 2 秒超时），任何可能阻塞的探测都会让 /health 超时，反而把
    整个【专家会诊】能力误判为离线。语义：ready = 已具备调用条件，不是"已验证连通"。
    """
    try:
        from acp_channel import hermes_available
        hermes_ready = bool(hermes_available())
        hermes_note = ("本机 hermes-agent 已安装（ACP 直通）" if hermes_ready
                       else "未检测到 hermes-agent")
    except Exception as e:  # noqa: BLE001
        hermes_ready, hermes_note = False, f"探测失败: {e}"
    # ready 三态：True=已确认可用；False=确定不可用（未配置）；None=已配置但未验证。
    # 只有 hermes 是文件级确定性判断；WorkBuddy/KHA 需联网验证，本端点刻意不做
    # （见上方超时理由），故一律报 None 而非 True —— 不把"配了 key"说成"能力可用"。
    wb_configured = bool(os.getenv("WB_CLIENT_ID") and os.getenv("WB_CLIENT_SECRET"))
    kha_configured = bool(os.getenv("MOONSHOT_API_KEY"))
    return {
        "hermes": {"ready": hermes_ready, "note": hermes_note},
        "workbuddy": {
            "ready": None if wb_configured else False,
            "note": ("已配置开放平台凭证，未验证授权状态" if wb_configured
                     else "未配置 WB_CLIENT_ID/WB_CLIENT_SECRET")},
        "kha": {
            "ready": None if kha_configured else False,
            "note": ("已配置 MOONSHOT_API_KEY，未验证企业认证状态" if kha_configured
                     else "未配置 MOONSHOT_API_KEY")},
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mode": os.getenv("CR_MODE", "fake"),
            "dry_run_post": DRY_RUN_POST, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "channels": _channel_status()}


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


# 在途评审登记：idem_key → (threading.Event, run_id 或 None)。
# compare_run 是同步阻塞 handler，uvicorn 在线程池里并发跑；后到请求等在 Event 上，
# 首个请求完成后即可拿到同一个 run_id（2026-09-20 幂等去重）。
_INFLIGHT: dict = {}


def _run_summary(state: dict, dedup: bool = False) -> dict:
    out = {"run_id": state["run_id"],
           "status": state.get("status") or (
               "pending_human" if state.get("pending_human") else "resolved"),
           "verdict": state.get("verdict")}
    if dedup:
        out["deduplicated"] = True
    return out


@app.post("/compare/run")
def compare_run(body: CompareIn) -> dict:
    """手动触发一次 Compare（默认用内置样例 diff，便于演示）。

    带 idempotency_key 时：在途 → 等首个完成返回同一 run；已在库 → 直接返回历史 run；
    均未命中 → 正常执行并把键落库。
    """
    from code_review_compare_mvp import SAMPLE_CONTEXT, SAMPLE_DIFF
    idem = (body.idempotency_key or "").strip()[:64] or None

    if idem:
        with _db_lock:
            inflight = _INFLIGHT.get(idem)
        if inflight is not None:
            done, first_run_id = inflight
            done.wait(timeout=600)  # 与客户端超时同量级；超时则自己再跑一遍（兜底）
            if first_run_id[0]:
                state = get_run(first_run_id[0])
                if state:
                    return _run_summary(state, dedup=True)
        else:
            hit = find_run_by_idem(idem)
            if hit:
                state = get_run(hit["run_id"])
                if state:
                    return _run_summary(state, dedup=True)
            with _db_lock:
                _INFLIGHT[idem] = (threading.Event(), [None])

    event = CodeReviewEvent(provider="manual", repo="local/manual", mr_id="0",
                            title=(body.title or "手动提交的 diff 评审").strip())
    state = _execute(event, body.diff or SAMPLE_DIFF, body.context or SAMPLE_CONTEXT)
    if idem:
        state["idempotency_key"] = idem
        save_run(state)  # _execute 内已 save 过一次，此处带键重存（INSERT OR REPLACE）
        with _db_lock:
            ent = _INFLIGHT.pop(idem, None)
        if ent is not None:
            ent[1][0] = state["run_id"]
            ent[0].set()
    return _run_summary(state)


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


# ---------------------------------------------------------------- 归一技能层 REST
# 2026-09-20：供 personal-agent（Smart Agent）等本机调用方走 plain HTTP 使用
# 技能库。与 /mcp/ 下的同名工具共用 skillkit，行为一致（install 保持人工门）。
class SkillInstallIn(BaseModel):
    keyword: str
    auto: bool = False


def _flat_candidates(found: dict) -> list[dict]:
    """统一候选列表提取：cmd_search 返回嵌套结构
    {candidates: {keyword, total, candidates: [...]}}，且可能混入非 dict 条目。"""
    data = found.get("candidates") or []
    if isinstance(data, dict):
        data = data.get("candidates") or []
    return [c for c in data
            if isinstance(c, dict) and c.get("pkg") and c.get("skill")]


@app.get("/skills")
def skills_rest() -> dict:
    """列出 hub 本地技能库（名称/描述/触发词/来源）。"""
    import skillkit

    skills = skillkit.list_skills()
    return {"count": len(skills),
            "skills": [{"name": s.name, "description": s.description,
                        "triggers": s.triggers, "source": s.source, "dir": s.dir}
                       for s in skills]}


@app.get("/skills/search")
def skills_search_rest(keyword: str, top: int = 8) -> dict:
    """技能市场搜索（只搜索不安装）。返回扁平候选列表。"""
    import skillkit

    found = skillkit.search_market(keyword, top=top)
    if not found.get("ok"):
        return found
    return {"ok": True, "keyword": keyword,
            "candidates": _flat_candidates(found)}


@app.post("/skills/install")
def skills_install_rest(body: SkillInstallIn) -> dict:
    """安装链：搜索 → 取源码 → 安全预审 →（pass 且 auto 才）落库。

    默认人工门：只出预审报告，不写入技能库。
    """
    import skillkit

    found = skillkit.search_market(body.keyword, top=5)
    if not found.get("ok"):
        return found
    cands = _flat_candidates(found)
    if not cands:
        return {"ok": False, "error": "市场无匹配候选"}
    best = cands[0]
    fetched = skillkit.fetch_skill(best["pkg"], best["skill"])
    if not fetched.get("ok"):
        return fetched
    result = {"ok": True, "candidate": best, "staging_dir": fetched["dir"],
              "audit": fetched["audit"], "installed": False}
    if fetched["audit"]["verdict"] != "pass":
        result["error"] = ("预审 %s，拒绝安装，等待人工拍板"
                           % fetched["audit"]["verdict"])
        return result
    if not body.auto:
        result["hint"] = "预审通过。确认安装请重试 auto=true"
        return result
    installed = skillkit.install_skill(fetched["dir"], auto=True)
    result.update({"installed": installed["ok"], "install": installed})
    return result


@app.get("/skills/{name}")
def skills_read_rest(name: str) -> dict:
    """读取某技能 SKILL.md 全文。"""
    import skillkit

    data = skillkit.get_skill(name)
    if "error" in data:
        raise HTTPException(404, data["error"])
    return data


@app.get("/skills/{name}/audit")
def skills_audit_rest(name: str) -> dict:
    """对已安装技能跑确定性安全预审。"""
    import skillkit

    return skillkit.audit_skill(name)


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
