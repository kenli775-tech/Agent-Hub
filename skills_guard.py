# -*- coding: utf-8 -*-
"""skills_guard —— 技能安全预审引擎（确定性、纯标准库、零 LLM 依赖）。

定位（务必先读）
----------------
这是【确定性预审 screening】，**不是安全认证**。它只能拦住已知形态的风险，
识别不了语义级恶意（例如把后门伪装成正常业务逻辑）。
因此 verdict 的准确含义是：

    pass  = 通过本判据集，未发现已知形态风险 —— 不等于"绝对安全"
    warn  = 存在需人工确认的行为（自带可执行代码、外部网络出站等），不得自动安装
    fail  = 命中红线，拒绝安装

设计取舍（2026-09-18）
---------------------
1. 判据全部形式化、可复现：不调 LLM，同一输入必然同一结论（上一轮故障里
   "评审结论不可复现"正是要根治的问题）。
2. 按【文件角色】加权，而不是一刀切：
   - 同一模式出现在代码文件里 = 真实可执行风险
   - 出现在文档里 = 提及/示例，降一级
   这样才不至于把"教人识别 eval 的安全技能"（skill-vetter）误判成危险技能，
   也不至于让一份正常技能的 README 里的 curl 示例卡死安装。
3. 判据宁少而准：宁可漏报交给人看，也不要滥报让人对告警脱敏。
   每条命中都带文件:行号 + 原文片段，结论可复核。

用法
----
    python tools/skills_guard.py audit <技能目录> [--json]
    python tools/skills_guard.py audit-all <父目录> [--json]
    python tools/skills_guard.py audit-all <父目录> --quiet     # 只看判定行
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ---------------------------------------------------------------- 常量

CODE_EXT = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash",
    ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".vbs", ".pl", ".rb",
    ".php", ".lua", ".r", ".sql", ".exe", ".dll", ".so", ".dylib", ".jar",
    ".msi", ".scr", ".com", ".app", ".wsf", ".hta",
}
DOC_EXT = {".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc", ".html", ".htm"}
DATA_EXT = {".json", ".yml", ".yaml", ".toml", ".csv", ".tsv", ".xml", ".ini",
            ".cfg", ".conf", ".lock", ".env"}

MAX_FILE_BYTES = 1_500_000     # 单文件扫描上限，超出只记体积不计内容
MAX_FILES = 300                # 文件数上限（超过记 quality 项）
MAX_TOTAL_MB = 5.0             # 总体积上限

# shebang 第一行豁免：`#!/usr/bin/env python3` 里的 /usr/bin 是标准写法，不是"写系统目录"。
# 2026-09-18 实测教训：不加这条豁免，18 条 fail 里有 13 条是这个误报。
SHEBANG_RE = re.compile(r"^#!")
# 代码文件里的注释行：注释不执行，命中降一级并标记（恶意方可能把 payload 藏注释里等运行期解析，
# 所以降级而非丢弃）。
COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*|<!--|--)")
# "写动词"守卫：只有系统路径与写语义同行出现，才算"写系统目录/注册表"。
# 单纯引用 C:\Windows\System32\powershell.exe（调用系统程序）不应判为风险。
WRITE_VERB_RE = re.compile(
    r"(?i)(>\s*\S|>>|open\s*\([^)]*['\"][wa]|write_text|write_bytes|"
    r"shutil\.(?:copy|move|rmtree)|\bcp\s|\bmv\s|\brm\s|\bchmod\s|\bchown\s|"
    r"\btee\s|Set-Content|Add-Content|Out-File|Remove-Item|New-Item|"
    r"Copy-Item|Move-Item|reg\s+add|New-ItemProperty|Set-ItemProperty)"
)
# 需要"写动词同行"守卫的规则
GUARDED_RULES = {"R3_system_write": WRITE_VERB_RE}
# 豁免词：同行出现即不算命中。例：Invoke-WebRequest 打 127.0.0.1 是本地调用，不是外联。
LOCAL_HOST_RE = re.compile(r"(?i)(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0|://127\.|\.local\b)")
EXEMPT_RULES = {"A1_network_out": LOCAL_HOST_RE}

# 占位符/示例值：命中密钥形态时用它排除"文档里写的假钥匙"
PLACEHOLDER_RE = re.compile(
    r"(?i)(x{4,}|y{4,}|0{6,}|your[_-]?|my[_-]?|example|sample|placeholder|"
    r"redact|dummy|fake|change[_-]?me|todo|<[^>]{1,40}>|\$\{|\.\.\.|abc123)"
)

SKILL_MD = "SKILL.md"

# Claude Code / 其他平台专有 frontmatter 键（Smart Agent 不认识，语义会丢）
FOREIGN_FM_KEYS = {
    "disable-model-invocation", "allowed-tools", "argument-hint", "model",
    "context", "agent", "hooks", "user-invocable", "license-note",
}

# ---------------------------------------------------------------- 判据表
#
# RED  : 明确的攻击面。代码文件命中 → fail；文档命中 → warn（需人核）
# AMBER: 需人工确认的行为。代码文件命中 → warn；文档命中 → info
#
# 正则说明：代码里通常写 C:\Windows 这种单反斜杠路径，正则用 \\ 匹配。

RED_RULES: list[tuple[str, str, re.Pattern]] = [
    ("R1_cred_path", "读取他人凭证/密钥文件位置", re.compile(
        r"(~[/\\]\.ssh|[/\\]\.ssh[/\\]|id_rsa|id_ed25519|"
        r"\.aws[/\\]credentials|\.aws[/\\]config|\.netrc|\.git-credentials|"
        r"Login Data|cookies\.sqlite|login\.keychain|\.pypirc|\.npmrc|"
        r"wallet\.dat|\.kube[/\\]config|keychains[/\\])")),
    ("R2_dynamic_exec", "动态执行外部输入（eval/exec 非字面量）", re.compile(
        r"\b(?:eval|exec)\s*\(\s*[A-Za-z_$]|"
        r"\bFunction\s*\(\s*[A-Za-z_$]|"
        r"Invoke-Expression\s+\$|"
        r"\biex\s*\(")),
    ("R3_system_write", "写系统目录/注册表", re.compile(
        r"(C:\\Windows|/etc/(?:passwd|shadow|sudoers|hosts|systemd)|"
        r"/usr/(?:bin|local|lib)/|Program Files|System32|"
        r"HKLM:|"
        r"(?i:hkcu\\software\\microsoft\\windows\\currentversion\\run))")),
    ("R4_persistence", "持久化驻留", re.compile(
        r"(?i)(schtasks\s+/create|Register-ScheduledTask|New-ScheduledTask|"
        r"crontab\s+-|\bLaunchAgents\b|\bLaunchDaemons\b|"
        r"systemctl\s+enable|/etc/systemd/system|shell:startup|"
        r"New-ItemProperty[^\n]{0,80}\\\\Run\b)")),
    ("R5_obfuscation", "混淆/编码后执行", re.compile(
        r"(?i)(b64decode|base64\s+(?:-d|--decode)|FromBase64String|"
        r"fromCharCode|atob\s*\(|"
        r"(?:\\x[0-9a-fA-F]{2}){8,})")),
    ("R6_live_secret", "疑似真实密钥/令牌", re.compile(
        r"(sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
        r"xox[baprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|"
        r"AIza[0-9A-Za-z_\-]{35}|tvly-[A-Za-z0-9]{16,}|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
        r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})")),
    ("R7_download_exec", "下载后直接执行", re.compile(
        r"(?i)((?:curl|wget)[^\n|]{0,140}\|\s*(?:sudo\s+)?(?:ba)?sh|"
        # iwr / Invoke-WebRequest 后面接的都是同一件事，简写与全写都要认
        # （实测漏写：只认 `| iex` 会漏掉 `| Invoke-Expression`）
        r"(?:iwr|Invoke-WebRequest)[^\n|]{0,200}\|\s*(?:iex|Invoke-Expression))")),
]

AMBER_RULES: list[tuple[str, str, re.Pattern]] = [
    ("A1_network_out", "外部网络出站", re.compile(
        r"(?i)(https?://(?!127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])[A-Za-z0-9.\-]+|"
        r"requests\.(?:post|put|patch)\s*\(|urlopen\s*\(|"
        r"Invoke-RestMethod|Invoke-WebRequest|"
        r"axios\.(?:post|put)|fetch\s*\(\s*[\"'`]https?://)")),
    ("A2_process_spawn", "执行外部进程/命令", re.compile(
        r"(?i)(\bsubprocess\.|\bos\.system\s*\(|\bos\.popen\s*\(|\bPopen\s*\(|"
        r"child_process|Start-Process|Invoke-Expression|shell\s*=\s*True|"
        r"\bshutil\.rmtree\b)")),
    ("A3_runtime_install", "运行期安装依赖", re.compile(
        r"(?i)(pip\s+install|npm\s+(?:i|install)\b|npx\s+--yes|brew\s+install|"
        r"winget\s+install|choco\s+install|apt-get\s+install|"
        r"Install-Module\s)")),
    ("A4_destructive_fs", "删除/覆写文件", re.compile(
        r"(?i)(rm\s+-rf|Remove-Item[^\n]{0,60}-Recurse|Format-Volume|"
        r"Clear-Disk|del\s+/[sq]\b|\bmkfs\b|\bdd\s+if=)")),
]

INFO_RULES: list[tuple[str, str, re.Pattern]] = [
    ("I1_env_read", "读取环境变量/系统信息", re.compile(
        r"(?i)(os\.environ|process\.env|\$env:|Get-ChildItem\s+Env:|"
        r"\bwhoami\b|systeminfo|Get-LocalUser|net\s+user\b)")),
    ("I2_shell_pipe", "shell 管道/拼接", re.compile(
        r"(?i)(\|\s*(?:ba)?sh\b|shell\s*=\s*True|Invoke-Expression|& \$\()")),
    # "提到 api_key 这个词"本身不是风险 —— 放在 AMBER 会让几乎每个联网技能都告警，
    # 结果是人对告警脱敏。降为 info 留痕即可。
    ("I3_credential_word", "正文涉及凭据/令牌字样", re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|"
        r"authorization:\s*bearer|password\s*[:=])")),
    ("I4_system_path_ref", "引用系统路径（调用而非写入）", re.compile(
        r"(C:\\Windows|/usr/(?:bin|local|lib)/|Program Files|System32)")),
]

# 疑似失效路径（迁移残留：OpenClaw / 旧工作区）
STALE_PATH_RE = re.compile(
    r"(?i)(~[/\\]\.openclaw|\.openclaw[/\\]|OpenClawWorkspaceBackup|"
    r"~/\.claude[/\\]|\.claude[/\\]skills)"
)

# ---------------------------------------------------------------- 数据结构


@dataclass
class Finding:
    rule: str
    label: str
    severity: str          # fail | warn | info
    file: str
    line: int
    snippet: str
    category: str = "security"   # security | quality

    def as_dict(self):
        return asdict(self)


@dataclass
class Report:
    skill: str
    path: str
    verdict: str = "pass"        # pass | warn | fail | error
    files: int = 0
    bytes: int = 0
    role_counts: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    exec_files: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def add(self, f: Finding):
        self.findings.append(f)

    @property
    def fails(self):
        return [f for f in self.findings if f.severity == "fail"]

    @property
    def warns(self):
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def infos(self):
        return [f for f in self.findings if f.severity == "info"]

    @property
    def quality(self):
        return [f for f in self.findings if f.category == "quality"]

    def finalize(self):
        if self.errors and self.verdict == "pass":
            pass
        if self.fails:
            self.verdict = "fail"
        elif self.warns:
            self.verdict = "warn"
        else:
            self.verdict = "pass"
        return self

    def as_dict(self):
        return {
            "skill": self.skill, "path": self.path, "verdict": self.verdict,
            "files": self.files, "bytes": self.bytes,
            "role_counts": self.role_counts,
            "counts": {"fail": len(self.fails), "warn": len(self.warns),
                       "info": len(self.infos),
                       "quality": len(self.quality)},
            "exec_files": self.exec_files,
            "meta": self.meta,
            "errors": self.errors,
            "findings": [f.as_dict() for f in self.findings],
        }


# ---------------------------------------------------------------- 基础工具

def classify_file(p: Path) -> str:
    """文件角色：code / doc / data / other。决定同一模式如何定级。"""
    ext = p.suffix.lower()
    if ext in CODE_EXT:
        return "code"
    if ext in DOC_EXT:
        return "doc"
    if ext in DATA_EXT:
        return "data"
    # 无扩展名但首行有 shebang 的脚本
    try:
        if not ext and p.stat().st_size < 200_000:
            first = p.open("rb").readline(200)
            if first.startswith(b"#!"):
                return "code"
    except OSError:
        pass
    return "other"


def read_text(p: Path) -> tuple[str, bool]:
    """读文本，返回 (内容, 是否被截断)。二进制/超大文件返回空。"""
    try:
        size = p.stat().st_size
    except OSError:
        return "", False
    if size > MAX_FILE_BYTES:
        return "", True
    raw = p.read_bytes()
    if b"\x00" in raw[:4096]:          # 二进制
        return "", False
    return raw.decode("utf-8", errors="ignore"), False


def _scan_pattern(text: str, rx: re.Pattern, role: str = "other", limit: int = 8
                  ) -> list[tuple[int, str, bool]]:
    """逐行匹配，返回 [(行号, 整行, 是否注释行)]，最多 limit 条（避免刷屏）。

    两条豁免/降级规则：
    - 首行 shebang 直接跳过（`#!/usr/bin/env python3` 不是"写 /usr/bin"）
    - 代码文件里的注释行标记出来，由调用方降一级
    """
    hits: list[tuple[int, str, bool]] = []
    for i, ln in enumerate(text.splitlines(), 1):
        s = ln.strip()
        if i == 1 and SHEBANG_RE.match(s):
            continue
        if not rx.search(ln):
            continue
        is_comment = role == "code" and bool(COMMENT_RE.match(s))
        hits.append((i, s, is_comment))
        if len(hits) >= limit:
            break
    return hits


def _is_placeholder(snippet: str) -> bool:
    return bool(PLACEHOLDER_RE.search(snippet))


# ---------------------------------------------------------------- 主审计逻辑

def audit_skill_dir(path: Path | str, for_install: bool = False) -> Report:
    """审计一个技能目录，返回 Report。纯函数式，不修改任何文件。

    for_install=True 表示"即将安装"场景：安装时会以 frontmatter name 作为落盘目录名，
    于是「name 与来源目录名不一致」「name 含非法字符」这两类问题会被自动规范化消解，
    降为 info（不阻止安装）。反之在体检**已装**技能时它们是真问题（主 Agent 定位不到
    目录、SKILL.md 读不到），保持 warn。
    """
    d = Path(path)
    rep = Report(skill=d.name, path=str(d))
    if not d.is_dir():
        rep.verdict = "error"
        rep.errors.append("目录不存在")
        return rep

    files = [p for p in sorted(d.rglob("*")) if p.is_file()]
    rep.files = len(files)
    rep.bytes = sum((p.stat().st_size for p in files), 0)
    roles: dict[str, int] = {"code": 0, "doc": 0, "data": 0, "other": 0}

    # --- 角色统计 + 可执行文件清单 ---
    for p in files:
        role = classify_file(p)
        roles[role] = roles.get(role, 0) + 1
        if role == "code":
            rel = p.relative_to(d).as_posix()
            if len(rep.exec_files) < 25:
                rep.exec_files.append(rel)
    rep.role_counts = roles

    # --- 逐文件扫模式 ---
    for p in files:
        role = classify_file(p)
        rel = p.relative_to(d).as_posix()
        # 二进制可执行文件：按后缀判定，不依赖内容 —— 小体积 .exe 会被当文本正常读出来，
        # 若把这段判断放在 content 分支里就会漏掉（实测 bin/tool.exe 含 "MZfake" 时漏判）。
        if p.suffix.lower() in (".exe", ".dll", ".so", ".dylib", ".msi", ".scr", ".com"):
            rep.add(Finding("R8_binary", "随包分发二进制可执行文件",
                            "fail", rel, 0, "%d bytes" % p.stat().st_size))
            continue
        text, truncated = read_text(p)
        if truncated:
            rep.add(Finding("Q_size", f"单文件超过 {MAX_FILE_BYTES // 1000}KB，未做内容扫描",
                            "info", rel, 0, "", category="quality"))
            continue
        if not text:
            continue

        for rule, label, rx in RED_RULES:
            guard = GUARDED_RULES.get(rule)
            for line_no, full, is_comment in _scan_pattern(text, rx, role):
                if guard is not None and not guard.search(full):
                    continue          # 缺"写动词"守卫：仅引用系统路径，不算写系统目录
                if rule == "R6_live_secret" and _is_placeholder(full):
                    continue
                sev = "fail" if role == "code" else "warn"
                if is_comment:
                    sev = "warn"      # 注释不执行，降一级但保留痕迹
                    full = "[注释] " + full
                rep.add(Finding(rule, label, sev, rel, line_no, full[:180]))

        for rule, label, rx in AMBER_RULES:
            exempt = EXEMPT_RULES.get(rule)
            for line_no, full, is_comment in _scan_pattern(text, rx, role):
                if exempt is not None and exempt.search(full):
                    continue          # 同行含 127.0.0.1 / localhost → 本地调用，非外联
                sev = "warn" if role == "code" else "info"
                if is_comment:
                    sev = "info"
                    full = "[注释] " + full
                rep.add(Finding(rule, label, sev, rel, line_no, full[:180]))

        for rule, label, rx in INFO_RULES:
            for line_no, full, _c in _scan_pattern(text, rx, role, limit=3):
                rep.add(Finding(rule, label, "info", rel, line_no, full[:180]))

        if role in ("doc", "other") and STALE_PATH_RE.search(text):
            m = STALE_PATH_RE.search(text)
            ln = text[:m.start()].count("\n") + 1
            rep.add(Finding("Q_stale_path", "正文引用疑似失效路径（迁移残留）",
                            "info", rel, ln, m.group(0)[:120], category="quality"))

    # --- 目录/元数据质量 ---
    _audit_quality(d, rep, for_install)

    # --- 汇总 ---
    return rep.finalize()


def _parse_fm(text: str) -> dict:
    """解析 SKILL.md frontmatter，支持 >- / |- 折叠块（与 server.py 同口径）。"""
    fm: dict[str, str] = {}
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return fm
    block = m.group(1)
    for km in re.finditer(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:\s*(.*)$", block, re.M):
        key, val = km.group(1), km.group(2).strip()
        if val in (">-", "|-", ">", "|"):
            rest = block[km.end():]
            nxt = re.search(r"\n\s*[A-Za-z_][A-Za-z0-9_\-]*\s*:", rest)
            body = rest[:nxt.start()] if nxt else rest
            val = " ".join(x.strip() for x in body.splitlines() if x.strip())
        fm[key] = val.strip().strip("\"'")
    return fm


def _audit_quality(d: Path, rep: Report, for_install: bool = False) -> None:
    """安装质量检查（不影响安全判定，但决定装进去能不能被正常使用）。

    for_install=True 时，落盘会被规范化的项降为 info，见 audit_skill_dir 文档。
    """
    md = d / SKILL_MD
    if not md.is_file():
        rep.add(Finding("Q_no_skill_md", "缺少 SKILL.md（扫描器会直接跳过，等于没装）",
                        "warn", SKILL_MD, 0, "", category="quality"))
        return

    raw = md.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        rep.add(Finding("Q_bom", "SKILL.md 带 UTF-8 BOM，frontmatter 正则会失配",
                        "warn", SKILL_MD, 0, "", category="quality"))
    text = raw.decode("utf-8", errors="ignore")
    fm = _parse_fm(text)

    name = fm.get("name", "")
    if not name:
        rep.add(Finding("Q_no_name", "frontmatter 缺 name",
                        "warn", SKILL_MD, 0, "", category="quality"))
    else:
        if name != d.name:
            rep.add(Finding("Q_name_mismatch",
                            f"frontmatter name({name}) ≠ 目录名({d.name})，"
                            "主 Agent 按 name 定位目录会失败"
                            + ("（安装时会按 name 落盘，此问题自动消解）" if for_install else ""),
                            "info" if for_install else "warn",
                            SKILL_MD, 0, f"name: {name}", category="quality"))
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
            rep.add(Finding("Q_name_illegal",
                            "name 含非法字符（安装接口只接受字母/数字/下划线/中划线）"
                            + ("（安装时会自动规范化为合法目录名）" if for_install else ""),
                            "info" if for_install else "warn",
                            SKILL_MD, 0, f"name: {name}", category="quality"))

    if not re.search(r"^description\s*:", text, re.M):
        rep.add(Finding("Q_no_desc", "frontmatter 缺 description",
                        "warn", SKILL_MD, 0, "", category="quality"))
    else:
        dm = re.search(r"^description\s*:\s*(.*)$", text, re.M)
        if dm and dm.group(1).strip() in (">-", "|-", ">", "|"):
            rep.add(Finding("Q_desc_block",
                            "description 用 YAML 折叠块，旧版扫描器会解析成标记本身",
                            "info", SKILL_MD, 0, dm.group(1).strip(), category="quality"))

    for k in sorted(set(fm) & FOREIGN_FM_KEYS):
        rep.add(Finding("Q_foreign_key", f"含其他平台专有 frontmatter 键：{k}",
                        "info", SKILL_MD, 0, f"{k}: {fm[k][:60]}", category="quality"))

    if not fm.get("license"):
        rep.add(Finding("Q_no_license", "未声明 license（无法确认分发授权）",
                        "info", SKILL_MD, 0, "", category="quality"))

    if rep.files > MAX_FILES:
        rep.add(Finding("Q_bulk_files", f"文件数 {rep.files} 超过 {MAX_FILES}",
                        "info", "", 0, "", category="quality"))
    if rep.bytes > MAX_TOTAL_MB * 1024 * 1024:
        rep.add(Finding("Q_bulk_size",
                        f"总体积 {rep.bytes / 1048576:.1f}MB 超过 {MAX_TOTAL_MB}MB",
                        "info", "", 0, "", category="quality"))


# ---------------------------------------------------------------- 渲染

VERDICT_CN = {"pass": "通过", "warn": "需人工确认", "fail": "拒绝安装", "error": "审计失败"}
VERDICT_TAG = {"pass": "[ PASS ]", "warn": "[ WARN ]", "fail": "[ FAIL ]", "error": "[ERROR!]"}


def render_text(rep: Report, verbose: bool = True) -> str:
    L: list[str] = []
    L.append("=" * 72)
    L.append("技能: %s" % rep.skill)
    L.append("路径: %s" % rep.path)
    L.append("判定: %s %s" % (VERDICT_TAG.get(rep.verdict, "?"),
                             VERDICT_CN.get(rep.verdict, "")))
    L.append("规模: %d 个文件 / %.1f KB   （代码 %d  文档 %d  数据 %d  其他 %d）"
             % (rep.files, rep.bytes / 1024,
                rep.role_counts.get("code", 0), rep.role_counts.get("doc", 0),
                rep.role_counts.get("data", 0), rep.role_counts.get("other", 0)))
    if rep.errors:
        L.append("错误: " + "；".join(rep.errors))
    L.append("")

    def block(title, items, show=6):
        if not items:
            return
        L.append("%s（%d）" % (title, len(items)))
        for f in items[:show]:
            loc = f"{f.file}:{f.line}" if f.line else f.file
            L.append("  · [%s] %s" % (f.rule, f.label))
            if loc.strip(":"):
                L.append("      %s" % loc)
            if f.snippet:
                L.append("      %s" % f.snippet)
        if len(items) > show:
            L.append("  · … 另有 %d 条同类命中，见 --json 输出" % (len(items) - show))
        L.append("")

    block("红线命中（fail）", rep.fails)
    block("需人工确认（warn）", rep.warns)
    if verbose:
        block("提示（info）", rep.infos, show=8)
        block("安装质量（不影响安全判定）", rep.quality, show=10)

    if rep.exec_files:
        L.append("随包可执行文件：")
        for f in rep.exec_files[:20]:
            L.append("  - %s" % f)
        if len(rep.exec_files) > 20:
            L.append("  - … 共 %d 个" % len(rep.exec_files))
        L.append("")

    L.append("说明：pass 仅表示通过本判据集（已知形态风险），不代表绝对安全。")
    L.append("      引用外部代码前，请人工复核上面列出的证据。")
    L.append("=" * 72)
    return "\n".join(L)


# ---------------------------------------------------------------- CLI

def _cmd_audit(args) -> int:
    rep = audit_skill_dir(args.target)
    if args.json:
        print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(rep, verbose=not args.brief))
    return {"pass": 0, "warn": 10, "fail": 20, "error": 30}.get(rep.verdict, 1)


def _cmd_audit_all(args) -> int:
    parent = Path(args.target)
    if not parent.is_dir():
        print("目录不存在: %s" % parent, file=sys.stderr)
        return 30
    subs = [p for p in sorted(parent.iterdir()) if p.is_dir()]
    reps = [audit_skill_dir(p) for p in subs]
    if args.json:
        print(json.dumps({"parent": str(parent),
                          "reports": [r.as_dict() for r in reps]},
                         ensure_ascii=False, indent=2))
    else:
        tally = {"pass": 0, "warn": 0, "fail": 0, "error": 0}
        for r in reps:
            tally[r.verdict] = tally.get(r.verdict, 0) + 1
        print("技能目录: %s" % parent)
        print("共 %d 个技能 —— 通过 %d / 需人确认 %d / 拒绝 %d / 异常 %d"
              % (len(reps), tally["pass"], tally["warn"], tally["fail"], tally["error"]))
        print("")
        for r in reps:
            if args.quiet:
                print("  %s %-34s fail=%d warn=%d"
                      % (VERDICT_TAG.get(r.verdict, "?"), r.skill,
                         len(r.fails), len(r.warns)))
                continue
            print(render_text(r, verbose=False))
    worst = max((r.verdict for r in reps), key=lambda v: {"pass": 0, "warn": 1,
                                                          "fail": 2, "error": 3}.get(v, 0),
                default="pass")
    return {"pass": 0, "warn": 10, "fail": 20, "error": 30}.get(worst, 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skills_guard",
                                 description="技能安全预审引擎（确定性判据，零 LLM）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="审计单个技能目录")
    a.add_argument("target")
    a.add_argument("--json", action="store_true")
    a.add_argument("--brief", action="store_true", help="只显示 fail/warn")
    a.set_defaults(func=_cmd_audit)

    b = sub.add_parser("audit-all", help="审计父目录下所有技能")
    b.add_argument("target")
    b.add_argument("--json", action="store_true")
    b.add_argument("--quiet", action="store_true", help="每条技能只输出一行判定")
    b.set_defaults(func=_cmd_audit_all)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
