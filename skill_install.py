# -*- coding: utf-8 -*-
"""skill_install —— 技能「搜索 → 取源码 → 安全审核 → 安装」编排器。

契约（对齐 2026-09-18 需求）
---------------------------
    搜索候选 → 按安装量排序 → 取源码到隔离区（不落地到技能目录）
    → skills_guard 确定性审计 → 全绿(pass) 才允许安装
    warn / fail 一律停下、出报告、等人工拍板

为什么做成确定性 CLI，而不是"丢给主 Agent 自由发挥"
--------------------------------------------------
2026-09-18 实测：让主 Agent 自己在工具循环里串这 6 步，交付的是
25,259 字符的重复代码块、6 轮预算耗尽、零安装——而且 status 仍报 completed。
搜索/解析/审计/落点每一步都是可形式化的，交给确定性代码才可复现、可验收。
主 Agent 只需调用本 CLI 一到两次即可完成整条链。

外部事实（2026-09-18 实测，勿凭记忆改写）
---------------------------------------
- `skills find <kw>` 结果稳定（连跑 3 次完全一致），**但绝不能加 `--json`**：
  加了这个未文档化的参数会命中另一套明显不完整的候选集
  （实测最高 installs 从 7.8K 掉到 1K）。
- `skills add` 支持 `--json` / `--copy` / `-y`；默认是 project 级落点，
  所以在隔离目录里跑它即可当"下载器"用，不必污染用户级 agent 目录。
- 技能的安装单位是 GitHub 仓库（owner/repo），不是 npm 包；
  查 npm 下载量 API 得不到任何东西。安装量数据只在 skills.sh。

命令
----
    python skill_install.py search <关键词> [--top N] [--json]
    python skill_install.py fetch <owner/repo> --skill <名字> [--workspace DIR]
    python skill_install.py audit <目录> [--json]
    python skill_install.py install <目录> [--auto] [--force]
    python skill_install.py run <关键词> [--top N] [--auto]

（agent-hub 归一技能层移植版，源自 personal-agent tools/skill_install.py；
技能落点为 <agent-hub>/skills/，不再写入豆包等第三方 Agent 目录。）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skills_guard import audit_skill_dir, render_text, _parse_fm  # noqa: E402

ROOT = Path(__file__).resolve().parent
EXIT = {"pass": 0, "warn": 10, "fail": 20, "error": 30}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# 完整 CSI 序列（含光标控制 `[1G` / `[?25l` / `[J`），ANSI_RE 只覆盖颜色码那一种
CSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# `owner/repo@skill-name   12.3K installs`
ENTRY_RE = re.compile(r"^(?P<pkg>[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)@(?P<skill>.+?)\s+"
                      r"(?P<num>[\d.]+)\s*(?P<unit>[KMB]?)\s+installs?\s*$")
URL_RE = re.compile(r"└\s*(https://skills\.sh/(?P<path>\S+))")
# `|    PDF OCR Extraction` —— skills CLI 的面板行
PANEL_RE = re.compile(r"^\|([ \t]*)([^|\n]*?)[ \t]*$", re.M)
UNIT = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


# ---------------------------------------------------------------- 工具函数

def _npx_env() -> dict:
    """保证 npx 可达：把常见 node 安装位置并入 PATH。"""
    env = dict(os.environ)
    extra = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
        Path.home() / ".workbuddy" / "binaries" / "node" / "versions",
    ]
    parts = [str(p) for p in extra if p.is_dir()]
    if parts:
        env["PATH"] = os.pathsep.join(parts + [env.get("PATH", "")])
    return env


def run_npx(args: list[str], cwd: Path | None = None, timeout: int = 180
            ) -> tuple[int, str]:
    """调 npx。Windows 上 npx 是 .cmd，必须经 cmd.exe，且用参数列表避免注入。"""
    cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", "npx", "--yes"] + args
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=_npx_env(),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "命令超时（%ds）：%s" % (timeout, " ".join(args))
    except OSError as e:
        return 127, "无法调用 npx：%r" % (e,)
    out = (p.stdout or b"").decode("utf-8", errors="replace")
    err = (p.stderr or b"").decode("utf-8", errors="replace")
    return p.returncode, ANSI_RE.sub("", out + ("\n" + err if err.strip() else ""))


def parse_find_output(text: str) -> list[dict]:
    """解析 `skills find` 输出为候选列表，按安装量降序。

    输出形如（已剥 ANSI）：
        k-dense-ai/scientific-agent-skills@liteparse 1K installs
        └ https://skills.sh/k-dense-ai/scientific-agent-skills/liteparse
    以 URL 行为准取 owner/repo/skill（同行可能有多个技能名，CLI 会用空格对齐，
    从 URL 拿到的 slug 最可靠）。
    """
    out: list[dict] = []
    lines = text.splitlines()
    cur: dict | None = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        m = ENTRY_RE.match(s)
        if m:
            cur = {
                "pkg": m.group("pkg"),
                "skill_raw": m.group("skill").strip(),
                "installs": int(float(m.group("num")) * UNIT[m.group("unit")]),
                "installs_text": "%s%s" % (m.group("num"), m.group("unit")),
                "url": "",
                "skill": "",
            }
            # 下一行的 URL 最可靠
            for j in range(i + 1, min(i + 3, len(lines))):
                um = URL_RE.search(lines[j])
                if um:
                    cur["url"] = um.group(1)
                    parts = um.group("path").split("/")
                    cur["skill"] = parts[-1] if parts else cur["skill_raw"]
                    cur["owner_repo"] = "/".join(parts[:2]) if len(parts) >= 2 else ""
                    break
            if not cur["skill"]:
                cur["skill"] = cur["skill_raw"]
            out.append(cur)
            continue
        um = URL_RE.search(s)
        if um and cur and not cur["url"]:
            cur["url"] = um.group(1)
            parts = um.group("path").split("/")
            cur["skill"] = parts[-1] if parts else cur["skill_raw"]
            cur["owner_repo"] = "/".join(parts[:2]) if len(parts) >= 2 else ""
    out.sort(key=lambda r: -r["installs"])
    return out


# ------------------------------------------------- slug ↔ 仓库内名字解析
#
# 实测坑（2026-09-18）：`skills find` 给的 slug 与仓库里 SKILL.md 的目录名/技能名
# **不一定一致**。例：find 输出 `claude-office-skills/skills@pdf-ocr-extraction`，
# 而仓库里该技能名叫 `PDF OCR Extraction`（带空格、词首大写）。
# 直接 `add -s pdf-ocr-extraction` 会失败（rc=1，只回一张可用技能清单）。
# 所以 fetch 首轮失败时必须回查清单、按 slug 化比对，再用仓库内真名重试一次。

def _slugify(name: str) -> str:
    """把任意技能名规整成 slug（小写、非字母数字压成单个中划线）。"""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def strip_csi(text: str) -> str:
    """剥掉全部 ANSI/CSI 转义序列（光标控制会把面板行切碎）。"""
    return CSI_RE.sub("", ANSI_RE.sub("", text))


def _parse_list_output(text: str) -> list[str]:
    """从 `skills add <pkg> --list` 输出里粗取候选技能名。

    面板行形如：
        |    PDF OCR Extraction          ← 名字/条目（缩进浅）
        |
        |      Extract text from scanned PDFs using optical character recognition
    交互失败时的另一种形态（只有名字，带 `- ` 前缀）：
        |    - PDF OCR Extraction
    这里**故意宽松**：描述行也可能被收进来。调用方按 slug 相似度过滤，
    假阳性自然被筛掉；宁可宽松也不漏真名。
    """
    clean = strip_csi(text)
    names: list[str] = []
    for _ind, val in PANEL_RE.findall(clean):
        v = val.strip()
        if v.startswith("-"):
            v = v[1:].strip()
        if v:
            names.append(v)
    return names


def _resolve_skill_name(pkg: str, slug: str, timeout: int = 180) -> list[str]:
    """在仓库清单里找与 slug 等价的仓库内名字，按置信度排序。

    返回 [] 表示清单里没有等价项（调用方应放弃重试，别瞎猜名字）。
    """
    rc, text = run_npx(["skills@latest", "add", pkg, "--list"], timeout=timeout)
    if rc != 0:
        return []
    names = _parse_list_output(text)
    want = _slugify(slug)
    if not want:
        return []
    exact = [n for n in names if _slugify(n) == want]
    loose: list[str] = []
    for n in names:
        s = _slugify(n)
        if not s or n in exact:
            continue
        if want in s or s in want:
            loose.append(n)
    # 去重保序（exact 优先）
    seen: set[str] = set()
    ordered: list[str] = []
    for n in exact + loose:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def _skills_root() -> Path:
    """hub 本地技能目录（唯一落点）：<agent-hub>/skills/

    与 personal-agent 不同，hub 的技能归 hub 自己持有（归一技能层），
    不写入任何第三方 Agent 的目录；可用 SKILL_ROOT 环境变量覆盖。
    """
    return Path(os.environ.get("SKILL_ROOT") or (ROOT / "skills"))


def _default_for_install(target: Path) -> bool:
    """未显式指定时自动定口径：不在技能目录里的（暂存区/下载目录）按安装前预审。

    这样 `fetch → audit → install` 这条自然动作链不会出现
    「audit 判 warn、install 却判 pass」的自相矛盾结论。
    """
    try:
        t = Path(target).resolve()
        root = _skills_root().resolve()
        return t != root and root not in t.parents
    except OSError:
        return True


def _default_workspace() -> Path:
    return ROOT / "workspaces" / ".skill-staging"


def _parse_vendor_scan(text: str, skill: str) -> dict:
    """抓取 skills CLI 自带的第三方供应链评估（Gen / Socket / Snyk）。

    实测输出形如：
        |             Gen               Socket            Snyk      |
        |  smart-ocr  Safe              0 alerts          Low Risk  |
    这只是一个额外参考信号，来源与算法不透明，**不作为安装判据**。
    """
    rx = re.compile(
        r"\|\s*%s\s+(?P<gen>\w+)\s+(?P<socket>\d+\s+alerts?|no\s+alerts)\s+"
        r"(?P<snyk>.+?)\s*\|" % re.escape(skill))
    m = rx.search(text)
    if not m:
        return {}
    return {"gen": m.group("gen"), "socket": m.group("socket"),
            "snyk": m.group("snyk").strip()}


# ---------------------------------------------------------------- search

@dataclass
class CmdResult:
    ok: bool
    code: int = 0
    message: str = ""
    data: dict = field(default_factory=dict)


def cmd_search(keyword: str, top: int = 10) -> CmdResult:
    rc, text = run_npx(["skills@latest", "find", keyword], timeout=180)
    if rc != 0:
        return CmdResult(False, rc, "搜索失败（rc=%d）：\n%s" % (rc, text[-2000:]))
    cands = parse_find_output(text)
    if not cands:
        return CmdResult(False, 0, "搜索无结果或输出格式变化，原始输出：\n%s" % text[-2000:])
    return CmdResult(True, 0, "", {
        "keyword": keyword, "total": len(cands), "candidates": cands[:top],
    })


# ---------------------------------------------------------------- fetch

def cmd_fetch(pkg: str, skill: str, workspace: Path | None = None,
              timeout: int = 300) -> CmdResult:
    """把技能源码取到隔离区（**不**写进 .user_skills）。

    做法：在一个干净的临时目录里执行 project 级 `skills add --copy`，
    让 CLI 自己完成下载/解包，我们再递归定位含 SKILL.md 的目录。
    这样与 CLI 内部落点实现解耦（落点变了也不影响）。
    """
    ws = Path(workspace) if workspace else _default_workspace()
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", pkg + "__" + skill)
    dest = ws / safe
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    rc, text = run_npx(["skills@latest", "add", pkg, "-s", skill, "-y", "--copy"],
                       cwd=dest, timeout=timeout)
    # 定位下载结果：递归找 SKILL.md（与 CLI 内部落点实现解耦）
    found = sorted({p.parent for p in dest.rglob("SKILL.md")})

    # 首轮失败 → slug 可能与仓库内名字不一致，回查清单后按真名重试一次
    resolved_from = ""
    if not found:
        for alt in _resolve_skill_name(pkg, skill):
            if alt == skill:
                continue
            rc2, text2 = run_npx(["skills@latest", "add", pkg, "-s", alt, "-y", "--copy"],
                                 cwd=dest, timeout=timeout)
            found = sorted({p.parent for p in dest.rglob("SKILL.md")})
            if found:
                rc, text = rc2, text2
                resolved_from, skill = skill, alt
                break

    if not found:
        return CmdResult(False, rc or 30,
                         "未取到源码（rc=%d）。该技能可能不在 GitHub，或 CLI 输出格式已变。\n"
                         "原始输出：\n%s" % (rc, text[-2500:]))
    # 优先 .agents/skills/<slug>（实测的主落点），其次 slug 化的名字匹配，最后第一个
    want = _slugify(skill)
    best = found[0]
    for f in found:
        if _slugify(f.name) == want and f.as_posix().endswith("/skills/" + f.name):
            best = f
            break
    else:
        for f in found:
            if _slugify(f.name) == want:
                best = f
                break

    vendor = {}
    for nm in ([skill, resolved_from] if resolved_from else [skill]):
        vendor = _parse_vendor_scan(text, nm)
        if vendor:
            break
    msg = ["源码已取到隔离区（**未安装**）：",
           "  落点: %s" % best,
           "  规模: %d 个文件 / %.1f KB"
           % (len([p for p in best.rglob("*") if p.is_file()]),
              sum(p.stat().st_size for p in best.rglob("*") if p.is_file()) / 1024)]
    if resolved_from:
        msg.insert(1, "  ⚠ slug 与仓库内名字不一致：`%s` → 已回查清单解析为 `%s`"
                   % (resolved_from, skill))
    if vendor:
        msg.append("  CLI 第三方供应链评估: Gen=%s  Socket=%s  Snyk=%s"
                   % (vendor.get("gen", "?"), vendor.get("socket", "?"),
                      vendor.get("snyk", "?")))
        msg.append("  （第三方评估仅作参考，不作为安装判据；判据以 skills_guard 审计为准）")
    if len(found) > 1:
        msg.append("  另定位到 %d 个同名副本（agent 目录多份）：%s"
                   % (len(found) - 1, ", ".join(str(x) for x in found if x != best)))
    return CmdResult(True, 0, "\n".join(msg), {
        "pkg": pkg, "skill": skill, "dir": str(best),
        "resolved_from": resolved_from,
        "all_found": [str(x) for x in found], "vendor_scan": vendor,
        "raw": text[-1500:],
    })


# ---------------------------------------------------------------- install

def _sanitize(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_\-]+", "-", (name or "").strip()).strip("-")
    return s or "unnamed-skill"


def _find_installed(target_root: Path, name: str) -> Path | None:
    """在技能目录里找"同一个技能"的既有落点 —— **不假设目录名 == 落盘名**。

    为什么不能只看 `(root / _sanitize(name)).exists()`（2026-09-18 实测踩到）：
    平台侧存在一个约 30s 轮询的规范化器，会把技能目录名改写成
    **frontmatter name 的小写形式**：
        `PDF-OCR-Extraction`（我们用 _sanitize 落的名字）
          → 约 33s 后变成 `pdf ocr extraction`（= name.lower()）。
    用探针目录可证伪复现过（`ZZ-Probe-1234` + `name: ZZ Probe 1234` → `zz probe 1234`）。
    既存技能 `speech to text transcription` 同规律。

    后果：按字面名判"是否已装"会**漏判**（`pdf ocr extraction` ≠ `PDF-OCR-Extraction`，
    空格与中划线不同），于是重复安装会在技能目录里留下**两份同一技能**。

    这里做三级匹配：① 目录名完全相同；② slug 化后等价；③ SKILL.md 的 frontmatter name 相同。
    """
    if not target_root.is_dir():
        return None
    want = _slugify(name)
    for p in sorted(target_root.iterdir()):
        if not p.is_dir():
            continue
        if p.name == name or (want and _slugify(p.name) == want):
            return p
        md = p / "SKILL.md"
        if not md.is_file():
            continue
        try:
            fm = _parse_fm(md.read_text(encoding="utf-8", errors="ignore")[:4000])
        except OSError:
            continue
        if (fm.get("name") or "").strip() == name:
            return p
    return None


def cmd_install(src: Path, auto: bool = False, force: bool = False,
                origin: dict | None = None) -> CmdResult:
    """审计 → 通过才安装。默认不自动装（需 --auto），warn/fail 一律拒绝。"""
    src = Path(src)
    # for_install=True：落盘会按 frontmatter name 规范化目录名，
    # 所以"来源目录名不匹配/含非法字符"这类问题不计入拦截条件。
    rep = audit_skill_dir(src, for_install=True)

    if rep.verdict != "pass":
        return CmdResult(False, EXIT.get(rep.verdict, 1),
                         "审计未通过（%s），拒绝安装。报告：\n\n%s"
                         % (rep.verdict, render_text(rep)),
                         {"verdict": rep.verdict, "report": rep.as_dict(),
                          "installed": False})
    if not auto:
        return CmdResult(False, EXIT["warn"],
                         "审计通过，但未指定 --auto，仅出报告不安装。\n\n%s"
                         % render_text(rep),
                         {"verdict": "pass", "report": rep.as_dict(),
                          "installed": False})

    # 目标名：优先 frontmatter name，非法则用目录名规范化
    md = src / "SKILL.md"
    fm = _parse_fm(md.read_text(encoding="utf-8", errors="ignore"))
    name = fm.get("name", "") or src.name
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name or ""):
        name = _sanitize(name)
    target_root = _skills_root()
    target_root.mkdir(parents=True, exist_ok=True)
    dst = target_root / name
    # 既存判定必须"认得出同一个技能"，不能按字面目录名比 —— 见 _find_installed
    existing = _find_installed(target_root, name)
    if existing is not None and not force:
        return CmdResult(False, EXIT["warn"],
                         "目标已存在：%s\n（与拟落点 %s 视为同一技能；用 --force 覆盖）"
                         % (existing, dst),
                         {"verdict": "pass", "installed": False, "reason": "exists",
                          "existing": str(existing)})
    if existing is not None and existing != dst:
        shutil.rmtree(existing, ignore_errors=True)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)

    # 安装溯源：记录来源与审计摘要，便于日后核对/更新
    meta = {
        "name": name,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": origin or {},
        "audit": {
            "verdict": rep.verdict,
            "files": rep.files, "bytes": rep.bytes,
            "counts": {"fail": len(rep.fails), "warn": len(rep.warns),
                       "info": len(rep.infos)},
        },
    }
    try:
        (dst / ".install-meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    # 落点自检：装完必须能被扫描器真的扫到
    verify = "OK"
    if not (dst / "SKILL.md").is_file():
        verify = "缺少 SKILL.md —— 扫描器会跳过，等于没装"
    return CmdResult(True, 0, "已安装：%s\n落点自检：%s" % (dst, verify),
                     {"verdict": "pass", "installed": True, "dest": str(dst),
                      "verify": verify, "meta": meta})


# ---------------------------------------------------------------- run（一条龙）

def cmd_run(keyword: str, top: int = 5, auto: bool = False,
            workspace: Path | None = None) -> CmdResult:
    """search → 取第一名候选源码 → 审计 → （全绿且 --auto 才）安装。"""
    log: list[str] = []
    s = cmd_search(keyword, top=top)
    if not s.ok:
        return CmdResult(False, s.code, s.message)
    cands = s.data["candidates"]
    log.append("搜索「%s」得到 %d 个候选，按安装量取前 %d："
               % (keyword, s.data["total"], len(cands)))
    for c in cands:
        log.append("  %-46s %8s installs   %s"
                   % (c["pkg"] + "@" + c["skill"], c["installs_text"],
                      "非 GitHub 源" if "." in c["pkg"].split("/")[0] else ""))

    best = cands[0]
    pkg, skill = best["pkg"], best["skill"]
    log.append("")
    log.append("→ 取第一名候选源码：%s@%s（%s installs）"
               % (pkg, skill, best["installs_text"]))
    f = cmd_fetch(pkg, skill, workspace=workspace)
    if not f.ok:
        log.append(f.message)
        return CmdResult(False, f.code, "\n".join(log),
                         {"candidates": cands, "fetched": False})

    log.append("   源码：%s" % f.data["dir"])
    rep = audit_skill_dir(Path(f.data["dir"]), for_install=True)
    log.append("")
    log.append("（审计口径：安装前预审 for_install=True）")
    log.append(render_text(rep))

    if rep.verdict != "pass":
        log.append("")
        log.append("判定为 %s —— 按约定不自动安装，等人工拍板。" % rep.verdict)
        return CmdResult(False, EXIT.get(rep.verdict, 1), "\n".join(log),
                         {"candidates": cands, "fetched": True,
                          "report": rep.as_dict(), "installed": False})

    if not auto:
        log.append("")
        log.append("审计全绿。未指定 --auto，停在安装前等确认。")
        return CmdResult(False, EXIT["warn"], "\n".join(log),
                         {"candidates": cands, "fetched": True,
                          "report": rep.as_dict(), "installed": False})

    ins = cmd_install(Path(f.data["dir"]), auto=True,
                      origin={"pkg": pkg, "skill": skill,
                              "installs": best["installs"],
                              "url": best.get("url", "")})
    log.append("")
    log.append(ins.message)
    return CmdResult(ins.ok, ins.code, "\n".join(log),
                     {"candidates": cands, "fetched": True,
                      "report": rep.as_dict(), "install": ins.data})


# ---------------------------------------------------------------- CLI

def _emit(res: CmdResult, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"ok": res.ok, "code": res.code,
                          "message": res.message, "data": res.data},
                         ensure_ascii=False, indent=2))
    else:
        if res.message:
            print(res.message)
        d = res.data
        if d.get("candidates") and "report" not in d:
            print("")
            print("候选（按安装量降序）：")
            for c in d["candidates"]:
                print("  %-46s %8s installs" % (c["pkg"] + "@" + c["skill"],
                                                c["installs_text"]))
    return res.code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="skill_install", description="技能搜索/取源码/审计/安装编排器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="按关键词搜索技能（按安装量排序）")
    p.add_argument("keyword")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("fetch", help="取源码到隔离区（不安装）")
    p.add_argument("pkg")
    p.add_argument("--skill", required=True)
    p.add_argument("--workspace")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("audit", help="审计目录（委托 skills_guard）")
    p.add_argument("target")
    p.add_argument("--for-install", dest="for_install", action="store_true",
                   help="按「安装前预审」口径：落盘会被规范化的命名项降为 info")
    p.add_argument("--installed", action="store_true",
                   help="按「已装技能体检」口径：命名不一致等保持 warn")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("install", help="审计通过才安装到技能目录")
    p.add_argument("target")
    p.add_argument("--auto", action="store_true", help="全绿时自动安装")
    p.add_argument("--force", action="store_true", help="覆盖同名技能")
    # 溯源信息（写进 .install-meta.json，便于日后核对/升级）
    p.add_argument("--pkg", default="", help="来源仓库 owner/repo")
    p.add_argument("--skill", default="", help="来源技能名")
    p.add_argument("--url", default="", help="来源 URL")
    p.add_argument("--installs", type=int, default=0, help="来源安装量")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="一条龙：搜索→取源码→审计→（全绿且 --auto 才）安装")
    p.add_argument("keyword")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--auto", action="store_true")
    p.add_argument("--workspace")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "search":
        return _emit(cmd_search(args.keyword, args.top), args.json)
    if args.cmd == "fetch":
        return _emit(cmd_fetch(args.pkg, args.skill,
                               Path(args.workspace) if args.workspace else None),
                     args.json)
    if args.cmd == "audit":
        target = Path(args.target)
        if args.for_install and args.installed:
            print("--for-install 与 --installed 互斥，只能给一个。")
            return 2
        if args.for_install:
            fi, why = True, "显式 --for-install（安装前预审口径）"
        elif args.installed:
            fi, why = False, "显式 --installed（已装技能体检口径）"
        else:
            fi = _default_for_install(target)
            why = ("自动判定：目标不在技能目录内 → 安装前预审口径"
                   "（落盘会规范化的命名项降为 info）" if fi else
                   "自动判定：目标已在技能目录内 → 已装体检口径（命名项保持 warn）")
        rep = audit_skill_dir(target, for_install=fi)
        if args.json:
            print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2))
        else:
            print("口径：%s\n" % why)
            print(render_text(rep))
        return EXIT.get(rep.verdict, 1)
    if args.cmd == "install":
        origin = {}
        if args.pkg:
            origin["pkg"] = args.pkg
        if args.skill:
            origin["skill"] = args.skill
        if args.url:
            origin["url"] = args.url
        if args.installs:
            origin["installs"] = args.installs
        return _emit(cmd_install(Path(args.target), args.auto, args.force,
                                 origin or None),
                     args.json)
    if args.cmd == "run":
        return _emit(cmd_run(args.keyword, args.top, args.auto,
                             Path(args.workspace) if args.workspace else None),
                     args.json)
    return 1


if __name__ == "__main__":
    sys.exit(main())
