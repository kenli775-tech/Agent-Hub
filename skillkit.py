# -*- coding: utf-8 -*-
"""skillkit —— agent-hub 归一技能层：hub 自己持有的 SKILL.md 技能库。

定位（2026-09-20，移植自 personal-agent 已验证路线）
----------------------------------------------------
各 Agent 平台（豆包/Kimi）没有开放通道驱动"对方 Agent + 对方原生技能"，
但它们的技能包本质是 SKILL.md 流程文档，不依赖平台本身。
本模块把技能统一收到 hub 本地：<agent-hub>/skills/<技能名>/SKILL.md，
由 hub 的编排层（模型调用前注入 SKILL.md）执行 —— 即"技能归一"，而非"驱动对方"。

技能生命周期（全部复用移植来的确定性工具）：
    市场搜索   skill_install.cmd_search（npx skills CLI，按安装量降序）
    取源码     skill_install.cmd_fetch（隔离区，不直接落库）
    安全预审   skills_guard.audit_skill_dir（确定性规则，pass 才允许安装）
    安装落库   skill_install.cmd_install（唯一落点 skills_root()）
    注入执行   get_skill()/render_prompt()（给模型上下文，模型按 SKILL.md 流程做）

安全默认值：不自动装 —— search/audit 随便调，install 链路上 guard 非 pass 即停。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from skills_guard import audit_skill_dir, render_text, _parse_fm
from skill_install import cmd_search, cmd_fetch, cmd_install, _skills_root

__all__ = ["skills_root", "list_skills", "get_skill", "render_prompt",
           "audit_skill", "search_market", "fetch_skill", "install_skill"]


@dataclass
class SkillInfo:
    name: str
    dir: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    source: str = ""           # 来源包（owner/repo），手写的为空


def skills_root() -> Path:
    """hub 本地技能目录（唯一落点）。SKILL_ROOT 环境变量可覆盖（测试用）。"""
    root = _skills_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_skills() -> list[SkillInfo]:
    """扫描技能库，返回所有带 SKILL.md 的技能（按名称排序）。"""
    out: list[SkillInfo] = []
    root = skills_root()
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        md = d / "SKILL.md"
        if not md.is_file():
            continue
        fm = _parse_fm(md.read_text(encoding="utf-8", errors="replace"))
        trig = fm.get("triggers") or fm.get("trigger") or ""
        out.append(SkillInfo(
            name=str(fm.get("name") or d.name),
            dir=str(d),
            description=str(fm.get("description") or ""),
            triggers=[t.strip() for t in str(trig).split(",") if t.strip()],
            source=str(fm.get("source") or ""),
        ))
    return out


def get_skill(name: str) -> dict:
    """读技能全文（SKILL.md）。返回 {name, path, content}；不存在返回 error 字段。"""
    for s in list_skills():
        if s.name == name or Path(s.dir).name == name:
            content = Path(s.dir, "SKILL.md").read_text(encoding="utf-8", errors="replace")
            return {"name": s.name, "path": s.dir, "description": s.description,
                    "triggers": s.triggers, "content": content}
    return {"error": f"技能不存在: {name}（已安装: {[s.name for s in list_skills()] or '无'}）"}


def render_prompt(name: str) -> str:
    """生成可注入模型上下文的技能说明片段；技能不存在返回空串（不阻断主流程）。"""
    sk = get_skill(name)
    if "error" in sk:
        return ""
    return (
        f"\n\n【技能: {sk['name']}】{sk['description']}\n"
        f"触发词: {', '.join(sk['triggers']) or '（无）'}\n"
        "请严格按以下 SKILL.md 流程执行：\n" + sk["content"][:8000]
    )


def audit_skill(name_or_dir: str) -> dict:
    """对已安装技能（或指定目录）跑确定性安全预审。for_install=False（库内审计）。"""
    p = Path(name_or_dir)
    if not p.exists():
        hit = get_skill(name_or_dir)
        if "error" in hit:
            return hit
        p = Path(hit["path"])
    rep = audit_skill_dir(p, for_install=False)
    return {"target": str(p), "verdict": rep.verdict, "files": rep.files,
            "findings": [f.as_dict() for f in rep.findings],
            "text": render_text(rep, verbose=True)}


def search_market(keyword: str, top: int = 8) -> dict:
    """技能市场搜索（npx skills CLI，按安装量降序）。需要本机有 node/npx。"""
    r = cmd_search(keyword, top=top)
    return {"keyword": keyword, "ok": r.ok, "candidates": r.data, "error": None if r.ok else r.message}


def fetch_skill(pkg: str, skill: str, workspace: Path | None = None) -> dict:
    """取技能源码到隔离区（不写技能库）。返回 {dir, audit}（取回即预审）。"""
    r = cmd_fetch(pkg, skill, workspace=workspace)
    if not r.ok:
        return {"ok": False, "error": r.message}
    staging = Path(r.data["dir"])
    rep = audit_skill_dir(staging, for_install=True)   # 安装前口径（更严）
    return {"ok": True, "pkg": pkg, "skill": skill, "dir": str(staging),
            "audit": {"verdict": rep.verdict, "files": rep.files,
                      "findings": len(rep.findings),
                      "text": render_text(rep, verbose=True)}}


def install_skill(src: str, auto: bool = False, force: bool = False) -> dict:
    """把（已过审的）目录安装进技能库。guard 非 pass 且未 force 时拒绝落库。"""
    r = cmd_install(Path(src), auto=auto, force=force)
    return {"ok": r.ok, "dir": r.data.get("dir"),
            "verdict": r.data.get("verdict"), "error": None if r.ok else r.message}
