# -*- coding: utf-8 -*-
"""
代码评审 Compare 引擎（MVP）
同一个 diff，多个 AI Agent 并行给出结构化 finding，
由本模块归一化、聚类、裁决，产出一份可执行的评审结论。

设计约束（来自多 AI 协作平台讨论）：
- 所有 Agent 必须返回同一个 JSON schema，否则进入 repair / NEEDS_EXPERIMENT。
- Judge 不做投票玩具：两家独立命中同一根因 = 高置信 cluster；
  独家 blocker 必须 needs_human=true，不自动下结论。
- 最终 recommendation 只有三档：approve / comment / request_changes。
- 任何外发动作都不在这里做，只生成 mr_comment_draft，等待人工确认。

运行：python code_review_compare_mvp.py   （内置冒烟测试）
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
VALID_SEVERITY = set(SEVERITY_ORDER)
VALID_CATEGORY = {
    "correctness", "concurrency", "security",
    "performance", "test", "style",
}


# ---------------------------------------------------------------- schema
@dataclass
class Finding:
    id: str
    agent: str
    severity: str
    file: str
    line_hint: int | None
    category: str
    message: str
    suggestion: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    needs_human: bool = False


@dataclass
class Review:
    agent: str
    summary: str
    findings: list[Finding] = field(default_factory=list)
    missing_context: list[str] = field(default_factory=list)
    approved: bool = False


@dataclass
class Cluster:
    cluster_id: str
    theme: str
    severity: str
    support: list[str]              # finding ids
    support_agents: list[str]
    against: list[str]
    merged_message: str
    action: str                     # 必须修改 / 需人工确认 / 建议修改 / 可选
    confidence: float
    needs_human: bool = False


@dataclass
class Verdict:
    recommendation: str             # approve | comment | request_changes
    reason: str


# ---------------------------------------------------------------- normalize
def _extract_json_block(text: str) -> dict | None:
    """从模型输出中提取第一个完整 JSON 对象（容忍 ```json 围栏）。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = m.group(1) if m else None
    if candidate is None:
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        break
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # 容错（2026-09-20 实测）：hermes 经 ACP 的输出会把单引号写成 \'（非法 JSON 转义），
    # 严格解析直接失败导致整家评审被丢弃。逐级降级：清理 \' → Python 字面量解析。
    try:
        return json.loads(candidate.replace("\\'", "'"))
    except json.JSONDecodeError:
        pass
    try:
        import ast
        data = ast.literal_eval(candidate)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def normalize_review(agent: str, raw: Any) -> Review:
    """把任意 Adapter 的原始输出归一化成 Review；失败抛 ReviewParseError。"""
    if isinstance(raw, str):
        data = _extract_json_block(raw)
        if data is None:
            raise ReviewParseError(f"[{agent}] 输出不是合法 JSON，无法归一化")
    elif isinstance(raw, dict):
        data = raw
    else:
        raise ReviewParseError(f"[{agent}] 输出类型不支持: {type(raw)}")

    findings: list[Finding] = []
    for i, f in enumerate(data.get("findings", [])):
        sev = str(f.get("severity", "")).lower()
        cat = str(f.get("category", "")).lower()
        if sev not in VALID_SEVERITY:
            raise ReviewParseError(f"[{agent}] finding#{i} severity 非法: {sev}")
        if cat not in VALID_CATEGORY:
            cat = "correctness"  # 归一容错，不拒绝整条
        findings.append(Finding(
            id=str(f.get("id") or f"{agent}_{i:03d}"),
            agent=agent,
            severity=sev,
            file=str(f.get("file", "")),
            line_hint=f.get("line_hint"),
            category=cat,
            message=str(f.get("message", "")),
            suggestion=str(f.get("suggestion", "")),
            confidence=float(f.get("confidence", 0.5)),
            evidence=[str(e) for e in f.get("evidence", [])],
            needs_human=bool(f.get("needs_human", False)),
        ))
    return Review(
        agent=agent,
        summary=str(data.get("summary", "")),
        findings=findings,
        missing_context=[str(x) for x in data.get("missing_context", [])],
        approved=bool(data.get("approved", False)),
    )


class ReviewParseError(ValueError):
    pass


# ---------------------------------------------------------------- theme / cluster
_THEME_RULES = [
    ("测试缺口", ["测试", "单测", "用例", "test", "断言"]),
    ("退款幂等", ["幂等", "idempot", "重复退款", "重复扣"]),
    ("并发安全", ["并发", "锁", "race", "竞态", "原子"]),
    ("测试缺口", ["测试", "单测", "用例", "test", "断言"]),
    ("发布回滚", ["回滚", "灰度", "发布", "开关", "降级"]),
    ("权限安全", ["权限", "鉴权", "越权", "注入", "敏感", "secret"]),
    ("稳定性", ["超时", "重试", "timeout", "熔断", "限流"]),
    ("性能", ["性能", "n+1", "索引", "慢查询", "复杂度"]),
]


def theme_of(f: Finding) -> str:
    text = f"{f.message} {f.suggestion} {f.file} {f.category}"
    low = text.lower()
    for theme, kws in _THEME_RULES:
        if any(k.lower() in low for k in kws):
            return theme
    return f"其他-{f.category}"


# 同一文件内可视为同一根因的主题族（blocker 存在时合并）
_MERGEABLE_THEMES = {"退款幂等", "并发安全", "其他-correctness", "其他-concurrency"}


def cluster_findings(reviews: list[Review]) -> list[Cluster]:
    """按主题聚类；同文件、同属正确性族且存在 blocker 的 finding 视为同一根因合并。

    这是启发式 MVP：生产环境应换成 embedding 相似度聚类。
    """
    items = [f for r in reviews for f in r.findings]
    themes = {f.id: theme_of(f) for f in items}

    by_file: dict[str, list[Finding]] = {}
    for f in items:
        by_file.setdefault(f.file, []).append(f)
    for file, fs in by_file.items():
        if not file:
            continue
        family = [f for f in fs if themes[f.id] in _MERGEABLE_THEMES]
        blockers = [f for f in family if f.severity == "blocker"]
        if len(family) >= 2 and blockers:
            target_theme = themes[max(blockers, key=lambda x: x.confidence).id]
            for f in family:
                themes[f.id] = target_theme

    buckets: dict[str, list[Finding]] = {}
    for f in items:
        buckets.setdefault(themes[f.id], []).append(f)

    clusters: list[Cluster] = []
    for idx, (theme, items) in enumerate(sorted(buckets.items()), 1):
        items.sort(key=lambda x: (SEVERITY_ORDER[x.severity], -x.confidence))
        top = items[0]
        support = [f.id for f in items]
        agents = sorted({f.agent for f in items})
        is_blocker = any(f.severity == "blocker" for f in items)
        multi_agent_blocker = is_blocker and len(agents) >= 2
        single_blocker = is_blocker and len(agents) == 1

        if multi_agent_blocker:
            action, needs_human = "必须修改", False
        elif single_blocker:
            # 独家 blocker：不下自动结论，升级人工
            action, needs_human = "需人工确认", True
        elif top.severity == "blocker":
            action, needs_human = "需人工确认", True
        elif top.severity == "major":
            action, needs_human = "建议修改", False
        else:
            action, needs_human = "可选", False

        conf = round(sum(f.confidence for f in items) / len(items), 2)
        clusters.append(Cluster(
            cluster_id=f"c_{idx:03d}",
            theme=theme,
            severity=top.severity,
            support=support,
            support_agents=agents,
            against=[],
            merged_message=f"{top.message}（建议：{top.suggestion}）",
            action=action,
            confidence=conf,
            needs_human=needs_human,
        ))
    return clusters


def detect_conflicts(clusters: list[Cluster]) -> list[dict]:
    """从聚类结果中提炼仍需人工裁决的分歧（MVP：同一主题出现 against 才记录）。"""
    conflicts = []
    for c in clusters:
        if c.against:
            conflicts.append({"theme": c.theme, "detail": c.against})
    return conflicts


# ---------------------------------------------------------------- judge
def judge(clusters: list[Cluster], conflicts: list[dict]) -> Verdict:
    must_fix = [c for c in clusters if c.action == "必须修改" and c.severity == "blocker"]
    human_gate = [c for c in clusters if c.needs_human and c.severity == "blocker"]

    if must_fix:
        return Verdict(
            recommendation="request_changes",
            reason=f"存在 {len(must_fix)} 个 blocker，且至少两家独立命中：" +
                   "、".join(c.theme for c in must_fix),
        )
    if human_gate:
        return Verdict(
            recommendation="request_changes",
            reason=f"存在 {len(human_gate)} 个独家 blocker，需人工确认后处理：" +
                   "、".join(c.theme for c in human_gate),
        )
    if any(c.severity in ("major", "minor") for c in clusters) or conflicts:
        return Verdict(recommendation="comment", reason="无 blocker，但有建议修改项或分歧需讨论")
    return Verdict(recommendation="approve", reason="未发现需要处理的问题")


# ---------------------------------------------------------------- report
def draft_mr_comment(clusters: list[Cluster], verdict: Verdict,
                     missing: list[str]) -> str:
    lines = [f"【多Agent评审结论】{verdict.recommendation} — {verdict.reason}", ""]
    order = {"必须修改": 0, "需人工确认": 1, "建议修改": 2, "可选": 3}
    for c in sorted(clusters, key=lambda x: (order.get(x.action, 9), SEVERITY_ORDER[x.severity])):
        tag = f"[{c.action}]" + ("(需人工)" if c.needs_human else "")
        lines.append(f"- {tag} ({c.severity}, 置信度 {c.confidence}) {c.theme}：{c.merged_message}")
        lines.append(f"  支持: {', '.join(c.support)}")
    if missing:
        lines += ["", "【缺失上下文】"] + [f"- {m}" for m in missing]
    lines += ["", "> 本评论由 Agent Hub 生成，已等待人工确认；未自动发送。"]
    return "\n".join(lines)


def run_compare(reviews: list[Review]) -> dict:
    """引擎主入口：归一化完成后的 Review 列表 -> 完整 Compare 结果。"""
    clusters = cluster_findings(reviews)
    conflicts = detect_conflicts(clusters)
    verdict = judge(clusters, conflicts)
    missing: list[str] = []
    for r in reviews:
        missing.extend([f"[{r.agent}] {m}" for m in r.missing_context])
    return {
        "reviews": [asdict(r) for r in reviews],
        "clusters": [asdict(c) for c in clusters],
        "conflicts": conflicts,
        "verdict": asdict(verdict),
        "missing_context": missing,
        "mr_comment_draft": draft_mr_comment(clusters, verdict, missing),
        "pending_human": True,
        "auto_comment": False,
    }


# ---------------------------------------------------------------- fake reviewers
SAMPLE_DIFF = """diff --git a/src/refund/service.py b/src/refund/service.py
new file mode 100644
@@ -0,0 +1,60 @@
+import logging
+from .db import get_conn
+
+log = logging.getLogger(__name__)
+
+def refund(order_id: str, refund_id: str, amount: int) -> dict:
+    conn = get_conn()
+    cur = conn.cursor()
+    # 先查后写：检查该 refund_id 是否已退款
+    row = cur.execute(
+        "SELECT status FROM refunds WHERE refund_id = ?", (refund_id,)
+    ).fetchone()
+    if row and row[0] == "SUCCESS":
+        return {"ok": False, "reason": "duplicate"}
+
+    # 调用支付渠道退款（无超时/重试控制）
+    resp = call_payment_channel(order_id, refund_id, amount)
+
+ cur.execute(
+        "INSERT INTO refunds(refund_id, order_id, amount, status) "
+        "VALUES (?, ?, ?, ?)",
+        (refund_id, order_id, amount, "SUCCESS" if resp.ok else "FAILED"),
+    )
+    conn.commit()
+    return {"ok": resp.ok}
+
+def call_payment_channel(order_id, refund_id, amount):
+    # TODO: 渠道对接，目前直接返回成功
+    return type("R", (), {"ok": True})()
"""

SAMPLE_CONTEXT = """仓库: acme/payment
模块: 退款服务 src/refund/
约定: 退款链路必须可回滚；db 层禁止先查后写，唯一约束兜底。
"""


def fake_review_kimi(diff: str, context: str) -> dict:
    return {
        "agent": "kimi",
        "summary": "本次变更主要风险在退款幂等与并发安全",
        "findings": [
            {
                "id": "kimi_001", "severity": "blocker",
                "file": "src/refund/service.py", "line_hint": 12,
                "category": "correctness",
                "message": "先查后写不幂等：并发重试同一 refund_id 会重复退款",
                "suggestion": "用唯一约束 + 状态机替代先查后写",
                "confidence": 0.86,
                "evidence": ["src/refund/service.py#L9-L20"],
                "needs_human": False,
            },
            {
                "id": "kimi_002", "severity": "major",
                "file": "tests/test_refund.py", "line_hint": None,
                "category": "test",
                "message": "缺并发场景单测，无法证明幂等性",
                "suggestion": "补同一 refund_id 并发重试的测试用例",
                "confidence": 0.8,
                "evidence": [],
                "needs_human": False,
            },
        ],
        "missing_context": ["是否已有 refunds 表唯一索引 DDL"],
        "approved": False,
    }


def fake_review_doubao(diff: str, context: str) -> dict:
    return {
        "agent": "doubao",
        "summary": "工程实现上并发与回滚风险偏高",
        "findings": [
            {
                "id": "doubao_002", "severity": "blocker",
                "file": "src/refund/service.py", "line_hint": 24,
                "category": "concurrency",
                "message": "高并发下唯一性校验与写入之间存在竞态窗口",
                "suggestion": "DB 层加 UNIQUE(refund_id) 并捕获冲突异常",
                "confidence": 0.82,
                "evidence": ["src/refund/service.py#L9-L27"],
                "needs_human": False,
            },
            {
                "id": "doubao_003", "severity": "minor",
                "file": "src/refund/service.py", "line_hint": 26,
                "category": "style",
                "message": "存在缩进错误的行（行首多余空格）",
                "suggestion": "修正缩进并接入 lint 卡点",
                "confidence": 0.97,
                "evidence": ["src/refund/service.py#L26"],
                "needs_human": False,
            },
        ],
        "missing_context": [],
        "approved": False,
    }


def fake_review_workbuddy(diff: str, context: str) -> dict:
    return {
        "agent": "workbuddy",
        "summary": "按内部发布规范，缺回滚预案",
        "findings": [
            {
                "id": "workbuddy_001", "severity": "blocker",
                "file": "src/refund/service.py", "line_hint": None,
                "category": "correctness",
                "message": "退款链路未配置灰度与回滚开关，违反内部发布红线",
                "suggestion": "补灰度比例配置与一键回滚预案后再评审",
                "confidence": 0.6,
                "evidence": ["内部规范<发布红线-R12>"],
                "needs_human": True,
            },
        ],
        "missing_context": ["是否有发布审批单号"],
        "approved": False,
    }


FAKE_REVIEWERS = {
    "kimi": fake_review_kimi,
    "doubao": fake_review_doubao,
    "workbuddy": fake_review_workbuddy,
}


# ---------------------------------------------------------------- smoke test
def _assert_mvp(result: dict) -> None:
    clusters = result["clusters"]
    idem = [c for c in clusters if c["theme"] == "退款幂等"]
    assert idem, "应存在『退款幂等』cluster"
    c0 = idem[0]
    assert c0["severity"] == "blocker", "幂等 cluster 应为 blocker"
    assert set(c0["support"]) == {"kimi_001", "doubao_002"}, \
        "Kimi 与豆包应独立命中同一幂等问题并被合并"
    assert c0["action"] == "必须修改"

    wb = [c for c in clusters if c["theme"] == "发布回滚"]
    assert wb and wb[0]["needs_human"] is True, "独家 blocker 必须升级人工"

    assert result["verdict"]["recommendation"] == "request_changes"
    assert result["mr_comment_draft"]
    assert result["pending_human"] is True
    assert result["auto_comment"] is False


def smoke() -> dict:
    reviews = [normalize_review(name, fn(SAMPLE_DIFF, SAMPLE_CONTEXT))
               for name, fn in FAKE_REVIEWERS.items()]
    result = run_compare(reviews)
    _assert_mvp(result)
    return result


if __name__ == "__main__":
    r = smoke()
    print("smoke OK")
    print(f"recommendation: {r['verdict']['recommendation']}")
    print(f"原因: {r['verdict']['reason']}")
    print("\n--- MR 评论草稿 ---")
    print(r["mr_comment_draft"])
