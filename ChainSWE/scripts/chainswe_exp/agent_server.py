#!/usr/bin/env python3
"""JSON-lines bridge that keeps a SWE-Edit agent alive for SEQ+MEM.

This file is copied into each benchmark container. Protocol messages arrive on
stdin; exactly one line prefixed with CHAINSWE_AGENT_RESULT= is emitted for
each request. Other stdout is treated as diagnostic output by the host runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

from sweedit.core.agenthub import (
    SwebenchAgent,
    SwebenchAgentV2,
    SwebenchAgentWithLLMEditor,
    SwebenchAgentWithLLMFileEditor,
)

PREFIX = "CHAINSWE_AGENT_RESULT="


def make_agent(agent_type: str, session_id: str, reasoning_effort: str, save_dir: str):
    kwargs = {
        "session_id": session_id,
        "reasoning_effort": reasoning_effort,
        "save_dir": save_dir,
    }
    if agent_type == "baseline":
        return SwebenchAgent(**kwargs)
    if agent_type == "baseline-v2":
        return SwebenchAgentV2(**kwargs)
    if agent_type == "llm-file-editor":
        return SwebenchAgentWithLLMFileEditor(**kwargs)
    if agent_type == "llm-editor":
        return SwebenchAgentWithLLMEditor(**kwargs)
    raise ValueError(f"Unsupported agent_type: {agent_type}")


async def serve() -> None:
    agent = None
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            if request.get("command") == "shutdown":
                print(PREFIX + json.dumps({"status": "shutdown"}), flush=True)
                return
            if request.get("command") != "run":
                raise ValueError("Expected command=run or command=shutdown")

            if agent is None:
                agent = make_agent(
                    request["agent_type"],
                    request["session_id"],
                    request["reasoning_effort"],
                    request["save_dir"],
                )
                agent.set_workdir(request["workdir"])
            elif request.get("reset_context"):
                agent.reset()
                agent.set_workdir(request["workdir"])

            before = agent.get_token_stats()
            status = "completed"
            error = None
            try:
                await asyncio.wait_for(
                    agent.run(user_message=request["prompt"]),
                    timeout=float(request["timeout"]),
                )
            except TimeoutError:
                status = "timeout"
            except BaseException as exc:
                status = "error"
                error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc(file=sys.stderr)

            after = agent.get_token_stats()
            response = {
                "status": status,
                "error": error,
                "model": agent.get_model(),
                "token_stats_before": before,
                "token_stats_after": after,
                "tool_call_stats": agent.get_tool_call_stats(),
                "trajectory_dir": str(Path(request["save_dir"]) / request["session_id"]),
            }
            print(PREFIX + json.dumps(response, ensure_ascii=False), flush=True)
        except BaseException as exc:
            traceback.print_exc(file=sys.stderr)
            print(
                PREFIX
                + json.dumps(
                    {"status": "bridge_error", "error": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                ),
                flush=True,
            )


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    asyncio.run(serve())
