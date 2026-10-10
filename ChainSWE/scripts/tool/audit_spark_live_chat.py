#!/usr/bin/env python3
"""Bounded live Chat audit. Never saves credentials or executes text tool calls.

agent/full-first load the installed, unmodified SWE-Edit wheel. replay bypasses
SWE-Edit and sends a captured request with stdlib urllib. Agent execution is
restricted to a single harmless printf command and the existing finish tool.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

from probe_openai_gateway_tool_calls import load_dotenv, request_once, endpoint_for

COMMAND = "printf '%s\\n' CHAIN_SWE_LIVE_7391"


def shell_prompt(shell_name):
    return (
        "This is a tool-transport diagnostic, not a repository editing task. "
        "First call " + shell_name + " to execute exactly: " + COMMAND + ". "
        "After reading the real tool result, call finish with message equal to the "
        "printed marker. Do not call file_editor or any other shell command."
    )


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def response_summary(body):
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    calls = message.get("tool_calls") or []
    return {
        "response_id": body.get("id"), "finish_reason": choice.get("finish_reason"),
        "native_count": len(calls),
        "names": [c.get("function", {}).get("name") for c in calls],
        "content": message.get("content"),
        "message_keys": list(message), "usage": body.get("usage"),
        "choice_extras": choice.get("provider_specific_fields"),
        "logprobs": choice.get("logprobs"),
    }


async def agent_case(args, env, out):
    os.environ.update(env)
    os.environ.update(API_TYPE="OPENAI", USE_RESPONSES_API="false")
    # Import only after configuring the pool; no fake model or fake responses.
    from sweedit.core.agenthub.swebench_agent import SwebenchAgent
    import sweedit

    agent = SwebenchAgent(session_id="live-audit", save_dir=str(out / "trajectory"))
    if args.shell_name != "run_shell":
        shell_tool = agent._tool_handlers.pop("run_shell")
        type(shell_tool).name = args.shell_name
        agent._tool_handlers[args.shell_name] = shell_tool
    agent.max_iterations = args.max_iterations
    agent.rate_limit_delay = 0
    workdir = out / "workdir"
    workdir.mkdir()
    agent.set_workdir(str(workdir))
    probe_file = str(workdir / "transport_marker.txt")
    read_command = f"cat {probe_file}"
    prompt = Path(args.prompt).read_text() if args.prompt else shell_prompt(args.shell_name)
    if args.task == "file":
        prompt = (
            "This is a tool-transport diagnostic using a new disposable text file, "
            "not a repository task. First use file_editor create to create " + probe_file +
            " with file_text exactly 'value=41\\n'. Then use file_editor str_replace "
            "on that file with old_str='41', new_str='42'. Then call run_shell with "
            "exactly this command: " + read_command + ". After reading the actual "
            "shell result, call finish with message exactly 'value=42'. "
            "Do not access any other file or run any other command."
        )
        agent.max_iterations = 6
    requests, responses, executions = [], [], []

    async def on_request(request):
        payload = json.loads(request.content)
        index = len(requests)
        item = {"index": index, "started": time.time(), "url": str(request.url)}
        requests.append(item)
        save(out / f"request_{index:02d}.json", payload)
        save(out / "requests.json", requests)
        print(f"{out.name}: request {index} sent", flush=True)

    async def on_response(response):
        raw = await response.aread()
        index = len(responses)
        (out / f"response_{index:02d}.raw.json").write_bytes(raw)
        item = {"index": index, "ended": time.time(), "status": response.status_code}
        try:
            item.update(response_summary(json.loads(raw)))
        except (ValueError, AttributeError, TypeError):
            item["invalid_json_response"] = True
        responses.append(item)
        save(out / "responses.json", responses)
        print(f"{out.name}: HTTP {response.status_code}, native={item.get('native_count')}, finish={item.get('finish_reason')}", flush=True)

    agent.llm._client._client.event_hooks = {"request": [on_request], "response": [on_response]}
    original_execute = agent._execute_tool

    async def guarded_execute(call):
        arguments = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
        allowed = call.name == "finish" or (
            call.name == args.shell_name and arguments.get("command") == COMMAND
        )
        if args.task == "file":
            allowed = call.name == "finish" or (
                call.name == args.shell_name and arguments.get("command") == read_command
            ) or (
                call.name == "file_editor" and arguments.get("path") == probe_file and (
                    arguments.get("command") == "view" or
                    (arguments.get("command") == "create" and arguments.get("file_text") in {"value=41\n", "value=41"}) or
                    (arguments.get("command") == "str_replace" and arguments.get("old_str") == "41" and arguments.get("new_str") == "42")
                )
            )
        item = {"name": call.name, "arguments": arguments, "allowed": allowed}
        if allowed:
            item["output"] = await original_execute(call)
        else:
            item["output"] = "Diagnostic safety guard: this call is outside the permitted printf/finish task."
        executions.append(item)
        save(out / "executions.json", executions)
        return item["output"]

    agent._execute_tool = guarded_execute
    meta = {
        "model": agent.get_model(), "api_type": agent.api_type,
        "use_responses_api": agent.use_responses_api,
        "python": sys.version, "sweedit_path": sweedit.__file__,
        "packages": {p: importlib.metadata.version(p) for p in ("sweedit", "openai", "anthropic", "httpx")},
        "task": args.task, "max_iterations": agent.max_iterations, "outer_timeout": args.timeout,
        "max_tokens": 32768, "safety_guard": "exact diagnostic commands/file operations and finish only",
    }
    start = time.time()
    try:
        if args.mode == "agent":
            await asyncio.wait_for(agent.run(prompt), args.timeout)
        else:
            prompt = Path(args.prompt).read_text()
            await agent.context.add_user_message(prompt)
            response = await asyncio.wait_for(agent.llm.call(agent.context.get_messages()), args.timeout)
            calls = agent._extract_tool_calls(response)
            await agent.context.add_llm_response(response, tool_calls=calls)
        meta["status"] = "completed"
    except Exception as exc:
        meta["status"] = "error"
        meta["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        meta.update(elapsed=time.time() - start, request_count=len(requests),
                    tool_stats=agent.get_tool_call_stats(), usage=agent.get_token_stats())
        actual_shell = any(e["name"] == args.shell_name and e["allowed"] and "CHAIN_SWE_LIVE_7391" in e["output"] for e in executions)
        actual_finish = any(e["name"] == "finish" and e["arguments"].get("message") == "CHAIN_SWE_LIVE_7391" for e in executions)
        meta["marker_roundtrip_success"] = actual_shell and actual_finish
        if args.task == "file":
            meta["file_roundtrip_success"] = (
                Path(probe_file).exists() and Path(probe_file).read_text() == "value=42\n" and
                any(e["name"] == args.shell_name and e["allowed"] and "value=42" in e["output"] for e in executions) and
                any(e["name"] == "finish" and e["arguments"].get("message") == "value=42" for e in executions)
            )
        save(out / "result.json", meta)
        await agent.llm._client.close()
    print(json.dumps(meta, ensure_ascii=False), flush=True)


def replay_case(args, env, out):
    payload = json.loads(Path(args.request).read_text())
    if args.variant == "logprobs":
        payload.update(logprobs=True, top_logprobs=0)
    elif args.variant == "stream":
        payload["stream"] = True
    elif args.variant == "drop-provider":
        for message in payload["messages"]:
            message.pop("provider_specific_fields", None)
    elif args.variant == "standard-history":
        for message in payload["messages"]:
            if message.get("role") == "assistant":
                for key in list(message):
                    if key not in {"role", "content", "tool_calls", "refusal", "name", "audio", "function_call"}:
                        message.pop(key)
    save(out / "request.json", payload)
    start = time.time()
    result = {"variant": args.variant, "started": start}
    try:
        status, headers, raw = request_once(endpoint=endpoint_for(env["OPENAI_BASE_URL"]),
            api_key=env["OPENAI_API_KEY"], payload=payload, timeout=args.timeout)
        (out / "response.raw.txt").write_bytes(raw)
        save(out / "response.headers.json", headers)
        result["http_status"] = status
        if args.variant == "stream":
            chunks = []
            for line in raw.decode().splitlines():
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    chunks.append(json.loads(line[5:]))
            save(out / "chunks.json", chunks)
            result["chunks"] = len(chunks)
            result["native_chunks"] = sum(bool(c.get("choices", [{}])[0].get("delta", {}).get("tool_calls")) for c in chunks if c.get("choices"))
            result["content"] = "".join(c.get("choices", [{}])[0].get("delta", {}).get("content") or "" for c in chunks if c.get("choices"))
        else:
            result.update(response_summary(json.loads(raw)))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed"] = time.time() - start
    save(out / "result.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["agent", "full-first", "replay"], required=True)
    p.add_argument("--env-file", default="/run/secrets/openai_chat.env")
    p.add_argument("--output", required=True)
    p.add_argument("--prompt")
    p.add_argument("--request")
    p.add_argument("--task", choices=["shell", "file"], default="shell")
    p.add_argument("--shell-name", choices=["run_shell", "execute_bash"], default="run_shell")
    p.add_argument("--variant", default="original", choices=["original", "logprobs", "stream", "drop-provider", "standard-history"])
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--max-iterations", type=int, default=4)
    args = p.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    env = load_dotenv(Path(args.env_file))
    if args.mode == "replay":
        replay_case(args, env, out)
    else:
        asyncio.run(agent_case(args, env, out))


if __name__ == "__main__":
    main()
