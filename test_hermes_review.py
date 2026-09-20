# -*- coding: utf-8 -*-
"""hermes 评审 adapter 集成测试：作为"带技能的专家"参与 agent-hub 多AI评审。

验证（2026-09-20）
-----------------
1. CR_MODE=real 时 get_adapters() 包含 hermes（本机已装则必在）
2. RealHermesAdapter.review() 对一个小 diff 返回结构合法的分析 JSON
"""

import os

os.environ["CR_MODE"] = "real"

from code_review_agent_hub import get_adapters  # noqa: E402

SAMPLE_DIFF = """\
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,4 +1,5 @@
 def divide(a, b):
+    # TODO: handle zero
     return a / b
"""


def main() -> int:
    adapters = get_adapters()
    print("已加载 adapters:", sorted(adapters))
    if "hermes" not in adapters:
        print("FAIL: hermes adapter 未加载")
        return 1

    t0 = __import__("time").time()
    result = adapters["hermes"].review(SAMPLE_DIFF, "计算器模块，内部工具使用")
    print(f"hermes review OK ({__import__('time').time()-t0:.0f}s):")
    import json
    print(json.dumps(result, ensure_ascii=False, indent=1)[:900])
    assert result.get("agent") == "hermes"
    assert "issues" in result or "verdict" in result or "summary" in result, "返回结构异常"
    print("\nHERMES ADAPTER INTEGRATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
