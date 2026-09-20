"""KHA 通道冒烟测试。

真实可用 → 跑一轮 ask() 验证端到端；
未开通企业认证（403）→ 打印明确原因并 SKIP（退出码 0，不算失败）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from kha_channel import kha_available, kha_unavailable_reason, _get_kha_client


def main() -> int:
    if not kha_available():
        print(f"SKIP: kimi-hosted 不可用 — {kha_unavailable_reason()}")
        return 0
    client = _get_kha_client()
    agents = client.list_agents()
    print(f"智能体 {len(agents)} 个: "
          + ", ".join(f"{a.get('name')}({a.get('id')})" for a in agents[:5]))
    reply = client.ask("用一句话介绍你自己，然后停止。", timeout_s=120)
    print(f"ask() 真实回复（{len(reply)} 字符）: {reply[:200]}")
    print("PASS: KHA 端到端通道可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
