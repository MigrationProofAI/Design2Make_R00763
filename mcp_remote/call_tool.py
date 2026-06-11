#!/usr/bin/env python3
"""
call_tool.py - a tiny CLI MCP client. Calls a tool on the running planning server
over SSE, so you can fire run_mrp / create_demand from the command line without the
agent or the Inspector.

    pip install "mcp[cli]"
    python call_tool.py run_mrp 11070 1710
    python call_tool.py run_mrp 11070            # plant defaults to 1710 in the tool
    python call_tool.py create_demand 11070

The planning server (sap_planning_mcp.py) must already be running on :8001.
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client

URL = "http://127.0.0.1:8001/sse"


async def main(tool: str, args: dict):
    async with sse_client(URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            structured = getattr(result, "structuredContent", None)
            if structured:
                print(json.dumps(structured, indent=2))
            else:
                for block in result.content:
                    print(getattr(block, "text", block))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("Usage: python call_tool.py <tool> <material> [plant]\n"
                 "  e.g. python call_tool.py run_mrp 11070 1710")
    tool, args = sys.argv[1], {"material": sys.argv[2]}
    if len(sys.argv) > 3:
        args["plant"] = sys.argv[3]
    asyncio.run(main(tool, args))
