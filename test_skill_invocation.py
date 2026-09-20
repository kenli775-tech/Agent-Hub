# -*- coding: utf-8 -*-
"""技能随行实测：评审任务必须调用 hermes 的 docx 技能读真实文件才能答对。

场景设计（2026-09-20）
--------------------
工作目录放 sample.docx（已知内容：合同金额 = 42 万元，付款方式 = 分期）。
diff 新增一个"合同摘要导出"函数，把金额字段写死成 100 万、付款方式写错。
评审问题"diff 中的导出结果与源文档是否一致"——不真读 docx 就无法发现不一致，
hermes 必须自主加载 docx 技能打开文件、核对原文，才能命中。
判定：
  1) 会话流里出现技能/工具调用痕迹（tool_call 更新或思路里提到技能）
  2) 回复中引用 docx 里的真实值（42 万 / 分期）——铁证技能真被执行了
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from docx import Document  # noqa: 托管运行时自带 python-docx

from acp_channel import HermesAcpClient

CONTRACT_TEXT = [
    ("合同金额", "人民币 42 万元整"),
    ("付款方式", "分期付款：签约 3 日内付 30%，验收后付尾款"),
    ("交付日期", "2026 年 10 月 31 日"),
]

DIFF = """\
diff --git a/contract_export.py b/contract_export.py
--- a/contract_export.py
+++ b/contract_export.py
@@ -0,0 +1,8 @@
+# 新增：合同摘要导出（声称数据来自 sample.docx）
+def export_summary():
    return {
+        "amount": "人民币 100 万元整",   # 从文档提取的金额
+        "payment": "一次性全款",          # 从文档提取的付款方式
+        "deliver_date": "2026-10-31",
+    }
"""

PROMPT = (
    "你是评审 Agent。工作目录里有一个 Word 文档 sample.docx，是下面这个 diff 所声称的数据来源。\n"
    "请先使用你能用的文档技能实际打开 sample.docx 读到里面的真实内容，"
    "然后评审这个 diff：导出函数声称的数值与源文档是否一致？不一致的地方列出来。\n"
    "回答里必须引用你从文档中读到的原文值作为证据。\n\n" + DIFF
)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="acp_skill_"))
    doc = Document()
    doc.add_heading("采购合同（样例）", level=1)
    for k, v in CONTRACT_TEXT:
        doc.add_paragraph(f"{k}：{v}")
    doc.save(str(workdir / "sample.docx"))
    print(f"工作目录: {workdir}（sample.docx 已生成，金额=42 万，付款=分期）")

    t0 = time.time()
    with HermesAcpClient(workdir=workdir, ask_timeout=240) as hermes:
        ch = hermes._ensure_connected()
        hermes._ensure_session(ch)
        n_before = len(ch.updates)
        print("prompt 已发送，等待 hermes 执行（含技能加载，最长 240s）...", flush=True)
        reply = hermes.ask(PROMPT)
        print(f"prompt 返回，用时 {time.time()-t0:.0f}s", flush=True)

        # 证据 1：会话流里的技能/工具调用痕迹
        kinds: dict[str, int] = {}
        tool_names: list[str] = []
        for u in ch.updates[n_before:]:
            upd = u.get("update") or {}
            kind = upd.get("sessionUpdate") or upd.get("type") or "?"
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind in ("tool_call", "tool_call_update"):
                tool_names.append(str(upd.get("title") or upd.get("kind") or upd))
        print(f"\n== 会话流更新类型: {kinds}")
        if tool_names:
            print(f"== 工具调用痕迹: {tool_names[:6]}")

    print(f"\n== hermes 回复 ({time.time()-t0:.0f}s) ==")
    print(reply[:1200])

    # 证据 2：回复引用了只有读过文件才知道的真实值
    hit_amount = "42" in reply and ("100" in reply or "不一致" in reply or "不符" in reply)
    hit_payment = "分期" in reply
    print("\n== 判定 ==")
    print(f"  引用真实金额 42 万并指出与 100 万不一致: {'✓' if hit_amount else '✗'}")
    print(f"  引用真实付款方式「分期」: {'✓' if hit_payment else '✗'}")
    ok = hit_amount and hit_payment
    print(f"\n{'SKILL INVOCATION VERIFIED' if ok else 'FAIL: 未见技能执行证据'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
