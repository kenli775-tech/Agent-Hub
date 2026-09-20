# -*- coding: utf-8 -*-
"""
方案共创模式（Proposal Collab）：
出题 → 三家 AI 独立出方案 → 交叉评审（每家评另外两家）→ 人工拍板
→ 产出 AI 编写定稿 → 三家基于定稿迭代优化 → 再拍板，直到满意。

模型：Kimi / MiniMax / DeepSeek（OpenAI 兼容接口）。
某家密钥缺失或调用失败不阻塞其他家，错误记录在 model_errors。
"""
from __future__ import annotations

import concurrent.futures
import os
import time

from real_model_adapters import _chat_completion

MODELS = [
    {"key": "kimi", "label": "Kimi",
     "env_key": "MOONSHOT_API_KEY", "env_model": "MOONSHOT_MODEL",
     "default_model": "kimi-k2.7-code",
     "env_base": "MOONSHOT_BASE_URL", "default_base": "https://api.moonshot.cn/v1"},
    {"key": "minimax", "label": "MiniMax",
     "env_key": "MINIMAX_API_KEY", "env_model": "MINIMAX_MODEL",
     "default_model": "MiniMax-Text-01",
     "env_base": "MINIMAX_BASE_URL", "default_base": "https://api.minimaxi.com/v1"},
    {"key": "deepseek", "label": "DeepSeek",
     "env_key": "DEEPSEEK_API_KEY", "env_model": "DEEPSEEK_MODEL",
     "default_model": "deepseek-chat",
     "env_base": "DEEPSEEK_BASE_URL", "default_base": "https://api.deepseek.com"},
    {"key": "qwen", "label": "千问",
     "env_key": "QWEN_API_KEY", "env_model": "QWEN_MODEL",
     "default_model": "qwen-plus",
     "env_base": "QWEN_BASE_URL",
     "default_base": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
]

DRAFT_SYSTEM = """你是资深方案架构师。针对用户的需求，输出一份完整、可落地的方案。
要求：
- 结构：背景理解 → 总体思路 → 分阶段实施步骤 → 资源与依赖 → 风险与对策 → 成功衡量指标
- 具体务实，避免空话；关键决策给出理由。
- 用 Markdown 输出，方案正文不超过 1500 字。"""

REVIEW_SYSTEM = """你是严格的方案评审专家。用户会给你两个由其他 AI 提出的方案。
对每个方案分别评审：
1. 优点（最多 3 条，要具体）
2. 缺陷与风险（按严重程度排序，必须指出可能被忽略的坑）
3. 改进建议（可操作的）
4. 两个方案如果只能采纳一个，你选哪个、为什么（不超过 100 字）
用 Markdown 输出，客观犀利，不要客套。"""

WRITEUP_SYSTEM = """你是方案撰写专家。用户选定了你之前提出的方案方向，并可能给了补充意见。
请输出该方案的最终定稿：完整、严谨、可直接执行的文档。
结构：背景与目标 → 方案概述 → 详细设计 → 实施计划（分阶段、可排期）→ 风险与应对 → 验收标准。
用 Markdown 输出。"""

ITERATE_SYSTEM = """你是方案优化专家。用户已经选定了一个方案方向（可能不是你最初提的），
现在请你基于自己的专业视角，对该方案进行一轮更新、优化或补强：
- 保留其主干，修复明显缺陷，吸收各家评审意见
- 补充你原方案中更好的设计
- 输出完整的新版本（不要只输出改动点），Markdown，不超过 1500 字。"""


def _cfg(m: dict) -> dict:
    return {"key": m["key"], "label": m["label"],
            "api_key": os.environ.get(m["env_key"], ""),
            "model": os.getenv(m["env_model"], m["default_model"]),
            "base": os.getenv(m["env_base"], m["default_base"])}


def _call(cfg: dict, system: str, user: str) -> str:
    if not cfg["api_key"]:
        raise RuntimeError(f"缺少 {cfg['label']} 的 API Key（环境变量未配置）")
    return _chat_completion(cfg["base"], cfg["api_key"], cfg["model"], [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])


def _parallel(calls: dict) -> dict:
    """calls: {key: (cfg, system, user)} → {key: text}；单家失败不阻塞。"""
    results, errors = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as ex:
        futs = {ex.submit(_call, cfg, s, u): k for k, (cfg, s, u) in calls.items()}
        for fut in concurrent.futures.as_completed(futs):
            k = futs[fut]
            try:
                results[k] = fut.result()
            except Exception as e:  # noqa: BLE001 - 记录错误，其余家照常
                errors[k] = f"{type(e).__name__}: {e}"[:500]
    return {"results": results, "errors": errors}


def draft_proposals(idea: str, context: str) -> dict:
    """Phase 1：三家独立出方案。"""
    brief = f"用户的想法/需求：\n{idea}\n\n补充背景：\n{context or '（无）'}"
    calls = {m["key"]: (_cfg(m), DRAFT_SYSTEM, brief) for m in MODELS}
    return _parallel(calls)


def cross_review(idea: str, context: str, plans: dict) -> dict:
    """Phase 2：每家评审另外两家的方案。"""
    brief_head = f"原始需求：\n{idea}\n\n补充背景：\n{context or '（无）'}\n\n"
    calls = {}
    for m in MODELS:
        others = [x for x in MODELS if x["key"] != m["key"] and x["key"] in plans]
        if not others:
            continue
        parts = [brief_head]
        for i, o in enumerate(others, 1):
            parts.append(f"【方案 {chr(64+i)} · 由 {o['label']} 提出】\n{plans[o['key']]}\n")
        calls[m["key"]] = (_cfg(m), REVIEW_SYSTEM, "\n".join(parts))
    return _parallel(calls)


def write_final_plan(cfg_key: str, idea: str, context: str,
                     plan: str, comment: str) -> str:
    """拍板后：由产出该方案的 AI 编写定稿。"""
    cfg = next(_cfg(m) for m in MODELS if m["key"] == cfg_key)
    user = (f"原始需求：\n{idea}\n\n补充背景：\n{context or '（无）'}\n\n"
            f"你此前提出的方案：\n{plan}\n\n"
            f"用户的补充意见：\n{comment or '（无）'}\n\n请输出最终定稿。")
    return _call(cfg, WRITEUP_SYSTEM, user)


def iterate_plans(idea: str, context: str, chosen: str,
                  comment: str, reviews: dict) -> dict:
    """迭代轮：三家各自基于选定方案产出优化版。"""
    review_text = "\n\n".join(
        f"【{next((m['label'] for m in MODELS if m['key']==k), k)} 的评审意见】\n{v}"
        for k, v in (reviews or {}).items()) or "（无评审记录）"
    user = (f"原始需求：\n{idea}\n\n补充背景：\n{context or '（无）'}\n\n"
            f"当前选定的方案：\n{chosen}\n\n"
            f"用户的迭代要求：\n{comment or '（无，请自行优化）'}\n\n"
            f"各家评审意见供参考：\n{review_text}")
    calls = {m["key"]: (_cfg(m), ITERATE_SYSTEM, user) for m in MODELS}
    return _parallel(calls)


def new_proposal_state(idea: str, context: str, proposal_id: str) -> dict:
    """完整跑 Phase1+Phase2，返回可持久化的状态字典。"""
    t0 = time.time()
    draft = draft_proposals(idea, context)
    plans, errors = draft["results"], draft["errors"]
    reviews, rerrors = {}, {}
    if len(plans) >= 2:
        rev = cross_review(idea, context, plans)
        reviews, rerrors = rev["results"], rev["errors"]
    errors.update(rerrors)
    return {
        "run_id": proposal_id,
        "mode": "proposal",
        "idea": idea,
        "context": context,
        "plans": plans,
        "reviews": reviews,
        "model_errors": errors,
        "phase": "awaiting_decision",
        "status": "awaiting_decision",
        "round": 1,
        "decision": None,          # {selected, comment, actor, final_plan, decided_at}
        "iterations": [],          # [{round, instruction, plans, reviews, model_errors, decided_at}]
        "event": {"provider": "proposal", "repo": "local/proposal", "mr_id": "",
                  "title": (idea or "方案共创")[:60]},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
    }
