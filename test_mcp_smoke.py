# -*- coding: utf-8 -*-
"""MCP 端点端到端冒烟测试：连接 /mcp → submit_review → 轮询 get_review。"""
import asyncio
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp/"
    token = sys.argv[2] if len(sys.argv) > 2 else "test-key-123"
    headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("tools:", names)
            assert "submit_review" in names and "get_review" in names

            r = await session.call_tool("submit_review", {"diff": "", "context": "冒烟测试"})
            run_id = r.content[0].text
            import json
            run_id = json.loads(run_id)["run_id"]
            print("run_id:", run_id)

            for i in range(30):
                await asyncio.sleep(1)
                g = await session.call_tool("get_review", {"run_id": run_id})
                state = json.loads(g.content[0].text)
                if state.get("status") != "running":
                    break
            print("status:", state.get("status"))
            print("verdict:", json.dumps(state.get("verdict"), ensure_ascii=False))
            assert state.get("verdict", {}).get("recommendation") == "request_changes"
            assert state.get("clusters"), "应返回聚类结果"
            print("MCP 端到端冒烟 OK")


asyncio.run(main())
