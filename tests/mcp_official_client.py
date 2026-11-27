#!/usr/bin/env python3
"""Talk to Referee's MCP server with the official MCP Python client.

Referee implements the protocol by hand, so this checks it against the
reference client. It needs the `mcp` package, which is why it is not part of
the dependency-free unit tests. Run it in a throwaway container from the
repository root, with the demo stack up (./demo/up.sh):

    docker run --rm -v "$PWD":/src:ro --env-file flagsmith.env \
      -e FLAGSMITH_API_URL=http://host.docker.internal:8000/api/v1 \
      python:3.12-slim sh -c "pip install -q mcp && python /src/tests/mcp_official_client.py"
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = {
    "name": "MCP client check",
    "hypothesis": "A plan sent through the official client is validated.",
    "flag": "mcp_client_check",
    "control": {"value": "old"},
    "variants": [{"key": "new", "value": "new", "share": 50}],
    "metrics": [{"event": "done", "goal": "increase"}],
    "assumptions": {"baseline_rate": 0.2, "minimum_lift": 0.25, "daily_identities": 1000},
}
FAILED = []


def check(label, condition, detail=""):
    print("  %s %s%s" % ("ok  " if condition else "FAIL", label, " -- %s" % detail if detail and not condition else ""))
    if not condition:
        FAILED.append(label)


async def main():
    env = {k: v for k, v in os.environ.items() if k.startswith("FLAGSMITH_")}
    env.update(PYTHONPATH=ROOT, REFEREE_STATE_DIR="/tmp/referee-mcp-check")
    params = StdioServerParameters(command=sys.executable, args=["-m", "referee", "mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = (await session.initialize()).model_dump(by_alias=True)
            check("handshake", init["serverInfo"]["name"] == "flagsmith-referee", init.get("serverInfo"))
            check("server instructions are sent", len(init.get("instructions") or "") > 200)
            tools = [t.name for t in (await session.list_tools()).tools]
            check("thirteen tools", len(tools) == 13, tools)
            check("no tool approves", not [t for t in tools if "approve" in t or "complete" in t], tools)
            prompts = [p.name for p in (await session.list_prompts()).prompts]
            check("the run_experiment prompt", prompts == ["run_experiment"], prompts)

            async def call(name, arguments):
                return (await session.call_tool(name, arguments)).model_dump(by_alias=True)

            result = await call("check_plan", {"plan": PLAN})
            check("check_plan validates and sizes a plan", not result["isError"] and result["structuredContent"]["valid"])
            result = await call("check_plan", {"plan": {"flag": "Bad Name"}})
            check("check_plan reports problems as data", result["structuredContent"]["valid"] is False)
            result = await call("approve", {})
            check("calling a tool that does not exist is an error result", result["isError"])
            result = await call("scorecard", {})
            check("a missing argument is explained", result["isError"] and "Missing required" in result["content"][0]["text"])
            if env.get("FLAGSMITH_API_KEY"):
                result = await call("referee_status", {})
                check("referee_status reaches Flagsmith", not result["isError"] and "Ready." in result["content"][0]["text"], result["content"][0]["text"])
                result = await call("list_experiments", {})
                check("list_experiments answers", not result["isError"])
            else:
                print("  (no Flagsmith credentials in the environment: skipped the calls that need them)")
    print("")
    print("%d check(s) FAILED" % len(FAILED) if FAILED else "All MCP client checks passed.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
