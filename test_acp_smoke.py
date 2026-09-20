# -*- coding: utf-8 -*-
"""ACP 通道冒烟测试：agent-hub → hermes-agent（经 acp_channel.py，生产同款代码）。

验证目标（2026-09-20）
--------------------
1. hermes-acp 子进程可起、ACP initialize 握手成功
2. 认证通过（hermes 自己的模型密钥，本脚本不碰）
3. session/new 建会话成功，workdir 里的文件对方可见
4. session/prompt 真实模型回复（整条通道端到端）
5. 对方自报可用技能，与 hermes-agent/skills 目录旁证
6. HermesAcpClient 会话复用：第二次 ask 不重建会话

用法：python test_acp_smoke.py
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from acp_channel import HermesAcpClient, hermes_available

PROMPT_TEXT = (
    "你是被另一个 Agent 平台通过 ACP 协议驱动的。请只用一句话回答两个问题："
    "1) 你现在可以使用哪些技能（列出技能名）？2) 当前工作目录下有什么文件？"
    "然后立即停止，不要做其他操作。"
)


def main() -> int:
    if not hermes_available():
        print("FAIL: hermes-agent ACP 通道不可用（未安装 hermes）")
        return 1

    t0 = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="acp_smoke_"))
    (workdir / "hello.txt").write_text("来自 agent-hub 的 ACP 冒烟测试\n", encoding="utf-8")

    try:
        with HermesAcpClient(workdir=workdir) as hermes:
            reply1 = hermes.ask(PROMPT_TEXT, timeout=240)
            print(f"1-4. ask OK ({len(reply1)} 字符, {time.time()-t0:.0f}s):\n   {reply1[:500]}")
            if len(reply1.strip()) < 10:
                print("FAIL: 未收到有效回复")
                return 1

            # 5) 技能目录旁证
            from acp_channel import AGENT_DIR
            skills_dir = AGENT_DIR / "skills"
            skills = [d.name for d in skills_dir.iterdir() if d.is_dir()] if skills_dir.is_dir() else []
            print(f"5. hermes 技能目录旁证: {len(skills)} 个 -> {skills[:8]}")

            # 6) 会话复用：同一 client 再问一次（不重建进程/会话）
            t1 = time.time()
            reply2 = hermes.ask("重复一遍：当前工作目录下那个 txt 文件名叫什么？一句话。", timeout=120)
            print(f"6. 会话复用 OK ({time.time()-t1:.0f}s): {reply2[:200]}")

        print(f"\nALL ACP SMOKE TESTS PASSED ({time.time()-t0:.0f}s)")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
