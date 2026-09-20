# -*- coding: utf-8 -*-
"""真实 diff 多 AI 评审对比（CR_MODE=real）：五家并行 → 归一 → 聚类 → 裁决。

2026-09-20：验证 hermes（ACP 直通，带技能库）与四家裸模型同场评审的归一/裁决链路。
用法：python test_real_compare.py
"""

import json
import os
import time

os.environ["CR_MODE"] = "real"
os.environ.setdefault("MODEL_TIMEOUT", "180")

from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).resolve().parent / ".env")

from code_review_agent_hub import CodeReviewEvent, get_adapters, run_compare_hub  # noqa: E402

DIFF = """\
diff --git a/user_api.py b/user_api.py
--- a/user_api.py
+++ b/user_api.py
@@ -1,18 +1,25 @@
 import sqlite3
+import logging

 def get_user(username):
     conn = sqlite3.connect("app.db")
     cur = conn.cursor()
-    cur.execute("SELECT * FROM users WHERE name = ?", (username,))
+    query = "SELECT * FROM users WHERE name = '%s'" % username
+    cur.execute(query)
     row = cur.fetchone()
+    password = row[2]
     conn.close()
     return row

 def average(scores):
     total = 0
     for s in scores:
         total += s
     return total / len(scores)
+
+def unused_helper(x):
+    return x * 2
"""
CONTEXT = "内部用户查询服务（Flask 路由直接调用这两个函数），SQLite 单文件库，无 ORM。"


def main() -> int:
    adapters = get_adapters()
    print("参与评审:", sorted(adapters))
    event = CodeReviewEvent(provider="manual", repo="local/demo", mr_id="0",
                            title="多AI真实对比（含 hermes ACP）")

    t0 = time.time()
    state = run_compare_hub(DIFF, CONTEXT, adapters=adapters, event=event)
    print(f"\n== 总耗时 {time.time()-t0:.0f}s | status={state.get('status')} ==")

    print("\n-- 各 Agent 问题数 / 错误 --")
    for c in state.get("clusters") or []:
        pass
    agent_errors = state.get("agent_errors") or {}
    for name, err in agent_errors.items():
        print(f"  {name}: ERROR {err}")

    print("\n-- 裁决 --")
    print(json.dumps({k: state.get(k) for k in ("verdict", "recommendation", "reason")},
                     ensure_ascii=False, indent=1)[:600])

    print("\n-- 问题聚类（前几组）--")
    for i, cl in enumerate((state.get("clusters") or [])[:8], 1):
        agents = cl.get("support_agents") or []
        print(f"  [{i}] {cl.get('severity', '?')} [{cl.get('action', '?')}] 来自={agents}")
        print(f"      {str(cl.get('merged_message', ''))[:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
