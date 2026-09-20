# -*- coding: utf-8 -*-
"""
Agent Hub MCP 端点：把多AI评审能力以 MCP 协议暴露给 WorkBuddy 等 MCP 客户端。

工具：
- submit_review(diff, context) → 立即返回 run_id（异步执行，真实模型约 1-2 分钟）
- get_review(run_id)          → 查询状态与结果（running / pending_human / resolved / failed）

挂载方式（agent_hub_platform 已自动挂载到 /mcp）：
    from agent_hub_mcp import mcp
    app.mount("/mcp", TokenAuthMiddleware(mcp.streamable_http_app()))

鉴权：HTTP Header Authorization: Bearer <AGENT_HUB_API_KEY>
（AGENT_HUB_API_KEY 未设置时不校验，仅本地开发）。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from mcp.server.fastmcp import FastMCP

# 内部端点路径设为 "/"：经 app.mount("/mcp", ...) 后，外部 /mcp/ 映射到内部 /
mcp = FastMCP("agent-hub", stateless_http=True, streamable_http_path="/")


def _new_run_id() -> str:
    return f"cr_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


@mcp.tool()
def submit_review(diff: str, context: str = "") -> dict:
    """提交一次多AI代码评审（Kimi/豆包/DeepSeek/WorkBuddy 并行 + 聚类裁决）。

    立即返回 run_id；评审在后台执行，真实模型约需 1-2 分钟，
    用 get_review(run_id) 轮询结果。diff 传空字符串则用内置样例。

    Args:
        diff: 统一 diff 格式的代码变更（空字符串 = 内置演示 diff）。
        context: 仓库约定、模块说明等评审上下文（可空）。
    """
    from code_review_agent_hub import CodeReviewEvent, get_adapters, run_compare_hub
    from agent_hub_platform import save_run

    run_id = _new_run_id()
    save_run({
        "run_id": run_id,
        "task_type": "code_review_compare",
        "event": {"provider": "mcp", "repo": "mcp-client", "mr_id": "0",
                  "title": "MCP 提交的多AI评审"},
        "status": "running",
        "pending_human": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    def _run() -> None:
        from code_review_compare_mvp import SAMPLE_CONTEXT, SAMPLE_DIFF
        try:
            event = CodeReviewEvent(provider="mcp", repo="mcp-client", mr_id="0")
            state = run_compare_hub(diff or SAMPLE_DIFF, context or SAMPLE_CONTEXT,
                                    adapters=get_adapters(), event=event)
            state["run_id"] = run_id
            save_run(state)
        except Exception as e:  # 失败也要落库，客户端能查到 failed 状态
            save_run({
                "run_id": run_id,
                "task_type": "code_review_compare",
                "event": {"provider": "mcp", "repo": "mcp-client", "mr_id": "0"},
                "status": "failed",
                "error": f"{type(e).__name__}: {e}",
                "pending_human": False,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

    threading.Thread(target=_run, daemon=True).start()
    return {"run_id": run_id, "status": "running",
            "hint": "用 get_review(run_id) 查询；真实模型约需 1-2 分钟"}


@mcp.tool()
def get_review(run_id: str) -> dict:
    """查询多AI评审结果。

    状态机：running（评审中）→ pending_human（出结论，含裁决/聚类/评论草稿）
    → resolved（人工已确认）；failed 表示执行出错。

    Args:
        run_id: submit_review 返回的 run_id。
    """
    from agent_hub_platform import get_run

    state = get_run(run_id)
    if not state:
        return {"error": f"run 不存在: {run_id}"}
    if state.get("status") == "running":
        return {"run_id": run_id, "status": "running"}
    if state.get("status") == "failed":
        return {"run_id": run_id, "status": "failed", "error": state.get("error")}

    result = {
        "run_id": run_id,
        "status": state.get("status", "pending_human"),
        "verdict": state.get("verdict"),
        "clusters": state.get("clusters"),
        "conflicts": state.get("conflicts"),
        "missing_context": state.get("missing_context"),
        "agent_errors": state.get("agent_errors"),
        "mr_comment_draft": state.get("mr_comment_draft"),
    }
    if state.get("human_decision"):
        result["human_decision"] = state["human_decision"]
        result["post_status"] = state.get("post_status")
    return result


# ---------------------------------------------------------------- 方案共创
def _new_proposal_id() -> str:
    return f"pp_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


@mcp.tool()
def submit_proposal(idea: str, context: str = "") -> dict:
    """方案共创 Phase1+2：出题 → Kimi/MiniMax/DeepSeek 三家独立出方案 → 交叉互评。

    立即返回 proposal_id；执行在后台进行（真实模型约 2-5 分钟），
    用 get_proposal(proposal_id) 轮询，phase 变为 awaiting_decision 即出结果。

    Args:
        idea: 想法/需求描述（必填，越具体方案越靠谱）。
        context: 补充背景：现状、约束、预算、期限等（可空）。
    """
    from agent_hub_platform import save_run

    idea = (idea or "").strip()
    if not idea:
        return {"error": "idea 不能为空"}
    proposal_id = _new_proposal_id()
    save_run({
        "run_id": proposal_id,
        "mode": "proposal",
        "phase": "running",
        "status": "running",
        "idea": idea,
        "context": context or "",
        "plans": {}, "reviews": {}, "model_errors": {},
        "round": 1, "decision": None, "iterations": [],
        "event": {"provider": "proposal", "repo": "mcp/proposal", "mr_id": "",
                  "title": idea[:60]},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    def _run() -> None:
        try:
            from proposal_collab import new_proposal_state
            state = new_proposal_state(idea, context or "", proposal_id)
            state["event"] = {"provider": "proposal", "repo": "mcp/proposal",
                              "mr_id": "", "title": idea[:60]}
            save_run(state)
        except Exception as e:  # noqa: BLE001
            save_run({
                "run_id": proposal_id, "mode": "proposal",
                "phase": "failed", "status": "failed",
                "idea": idea, "context": context or "",
                "error": f"{type(e).__name__}: {e}",
                "event": {"provider": "proposal", "repo": "mcp/proposal",
                          "mr_id": "", "title": idea[:60]},
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

    threading.Thread(target=_run, daemon=True).start()
    return {"proposal_id": proposal_id, "phase": "running",
            "hint": "用 get_proposal(proposal_id) 查询；真实模型约需 2-5 分钟"}


@mcp.tool()
def get_proposal(proposal_id: str) -> dict:
    """查询方案共创进度与结果。

    状态机：running（出方案+互评中）→ awaiting_decision（待用户拍板）
    → final（已定稿）；decide_proposal 拍板后可 iterate_proposal 发起新一轮迭代。

    Args:
        proposal_id: submit_proposal 返回的 ID。
    """
    from agent_hub_platform import get_run

    state = get_run(proposal_id)
    if not state or state.get("mode") != "proposal":
        return {"error": f"方案不存在: {proposal_id}"}
    if state.get("phase") == "running":
        return {"proposal_id": proposal_id, "phase": "running"}
    if state.get("phase") == "failed":
        return {"proposal_id": proposal_id, "phase": "failed",
                "error": state.get("error")}

    result = {
        "proposal_id": proposal_id,
        "phase": state.get("phase"),
        "round": state.get("round", 1),
        "idea": state.get("idea"),
        "context": state.get("context"),
        "plans": state.get("plans"),
        "reviews": state.get("reviews"),
        "model_errors": state.get("model_errors"),
    }
    if state.get("decision"):
        result["decision"] = state["decision"]
    if state.get("iterations"):
        result["iterations"] = [
            {"round": it.get("round"), "instruction": it.get("instruction")}
            for it in state["iterations"]]
    return result


@mcp.tool()
def decide_proposal(proposal_id: str, selected: str, comment: str = "") -> dict:
    """人工拍板：选定一家方案，由产出该方案的 AI 编写最终定稿。

    阻塞执行，通常 30-90 秒。之后可用 iterate_proposal 让三家基于定稿迭代。

    Args:
        proposal_id: 方案 ID。
        selected: 选定的方案作者，kimi / minimax / deepseek 之一。
        comment: 补充意见（可选），会交给产出 AI 写进定稿。
    """
    from agent_hub_platform import get_run, save_run
    from proposal_collab import write_final_plan

    state = get_run(proposal_id)
    if not state or state.get("mode") != "proposal":
        return {"error": f"方案不存在: {proposal_id}"}
    if state.get("phase") not in ("awaiting_decision", "final"):
        return {"error": f"当前阶段 {state.get('phase')} 不能拍板"}
    if selected not in (state.get("plans") or {}):
        return {"error": f"方案 {selected} 不存在，可选：{list((state.get('plans') or {}).keys())}"}
    try:
        final_plan = write_final_plan(selected, state["idea"],
                                      state.get("context", ""),
                                      state["plans"][selected], comment)
    except Exception as e:  # noqa: BLE001
        return {"error": f"定稿编写失败: {type(e).__name__}: {e}"}
    state["decision"] = {
        "selected": selected, "comment": comment, "actor": "mcp",
        "final_plan": final_plan,
        "decided_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state["phase"] = "final"
    state["status"] = "final"
    save_run(state)
    return {"proposal_id": proposal_id, "selected": selected,
            "phase": "final", "final_plan": final_plan}


@mcp.tool()
def iterate_proposal(proposal_id: str, instruction: str = "") -> dict:
    """迭代轮：三家 AI 基于当前定稿各自产出优化版 + 新一轮交叉评审。

    异步执行（约 2-5 分钟），完成后 phase 回到 awaiting_decision，再次拍板。

    Args:
        proposal_id: 已定稿的方案 ID。
        instruction: 本轮迭代要求（可选），如"重点压缩成本"。
    """
    from agent_hub_platform import get_run, save_run

    state = get_run(proposal_id)
    if not state or state.get("mode") != "proposal":
        return {"error": f"方案不存在: {proposal_id}"}
    chosen = (state.get("decision") or {}).get("final_plan")
    if not chosen:
        return {"error": "请先 decide_proposal 拍板，再发起迭代"}
    pid = proposal_id  # 闭包引用

    def _run() -> None:
        try:
            from proposal_collab import cross_review, iterate_plans
            prev = {"round": state.get("round", 1),
                    "instruction": (state.get("decision") or {}).get("comment", ""),
                    "plans": state.get("plans", {}),
                    "reviews": state.get("reviews", {}),
                    "model_errors": state.get("model_errors", {})}
            state.setdefault("iterations", []).append(prev)
            state["phase"] = "running"
            state["status"] = "running"
            save_run(state)
            t0 = time.time()
            it = iterate_plans(state["idea"], state.get("context", ""),
                               chosen, instruction, state.get("reviews", {}))
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
        except Exception as e:  # noqa: BLE001
            save_run({
                "run_id": pid, "mode": "proposal",
                "phase": "failed", "status": "failed",
                "error": f"{type(e).__name__}: {e}",
                "event": state.get("event", {}),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

    threading.Thread(target=_run, daemon=True).start()
    return {"proposal_id": proposal_id, "phase": "running",
            "hint": "迭代在后台执行，用 get_proposal 查询；约需 2-5 分钟"}


# ---------------------------------------------------------------- 归一技能层
# 2026-09-20：hub 本地技能库（<agent-hub>/skills/），移植自 personal-agent
# 已验证路线（skill_install + skills_guard）。模型侧通过 skill_read 取 SKILL.md
# 注入上下文执行；安全默认值 = search/audit 随便调，install 链路 guard 非 pass 即停。
@mcp.tool()
def skill_list() -> dict:
    """列出 hub 本地技能库已安装的技能（名称/描述/触发词/来源）。

    技能由 hub 统一持有与执行（归一技能层），不依赖任何外部 Agent 平台。
    """
    import skillkit

    skills = skillkit.list_skills()
    return {"count": len(skills),
            "skills": [{"name": s.name, "description": s.description,
                        "triggers": s.triggers, "source": s.source, "dir": s.dir}
                       for s in skills]}


@mcp.tool()
def skill_read(name: str) -> dict:
    """读取某个已安装技能的 SKILL.md 全文（供模型按流程执行）。

    Args:
        name: 技能名（skill_list 返回的 name，或技能目录名）。
    """
    import skillkit

    return skillkit.get_skill(name)


@mcp.tool()
def skill_audit(name: str) -> dict:
    """对已安装技能跑确定性安全预审（skills_guard 规则集）。

    返回 verdict（pass/warn/fail）+ 完整报告；日常巡检与安装前复核都用它。

    Args:
        name: 技能名或目录路径。
    """
    import skillkit

    return skillkit.audit_skill(name)


@mcp.tool()
def skill_search(keyword: str, top: int = 8) -> dict:
    """技能市场搜索（skills.sh / npx skills CLI），按安装量降序。

    只搜索不安装。需要本机有 node/npx。

    Args:
        keyword: 搜索关键词（英文效果好，如 pdf、ocr、scraping）。
        top: 返回候选数（默认 8）。
    """
    import skillkit

    return skillkit.search_market(keyword, top=top)


@mcp.tool()
def skill_install(keyword: str, auto: bool = False) -> dict:
    """技能安装链：市场搜索 → 取源码到隔离区 → 安全预审 → （guard pass 才）落库。

    默认人工门：预审出报告后停下，返回审计结论与隔离区路径，不写入技能库；
    auto=true 且 guard 判定 pass 时才真正安装。guard 为 warn/fail 一律拒绝。

    Args:
        keyword: 搜索关键词，取安装量最高的候选执行链路。
        auto: true=预审通过后直接安装；false=只出报告（默认，安全）。
    """
    import skillkit

    found = skillkit.search_market(keyword, top=5)
    if not found["ok"]:
        return found
    cands = [c for c in (found.get("candidates") or []) if c.get("pkg") and c.get("skill")]
    if not cands:
        return {"ok": False, "error": "市场无匹配候选", "candidates": found.get("candidates")}
    best = cands[0]
    fetched = skillkit.fetch_skill(best["pkg"], best["skill"])
    if not fetched.get("ok"):
        return fetched
    result = {
        "ok": True, "candidate": best, "staging_dir": fetched["dir"],
        "audit": fetched["audit"], "installed": False,
    }
    if fetched["audit"]["verdict"] != "pass":
        result["error"] = "预审 %s，拒绝安装，等待人工拍板" % fetched["audit"]["verdict"]
        return result
    if not auto:
        result["hint"] = "预审通过。确认安装请重试 skill_install(keyword, auto=true)"
        return result
    installed = skillkit.install_skill(fetched["dir"], auto=True)
    result.update({"installed": installed["ok"], "install": installed})
    return result
