#!/usr/bin/env python3
"""Probe an OpenAI-compatible Chat Completions gateway for native tool calls.

The probe deliberately uses a harmless function and never executes a returned
tool call. It saves the raw HTTP body before optionally parsing the same body
with the installed OpenAI SDK, making it possible to distinguish gateway
serialization failures from SDK/scaffolding extraction failures.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SAFE_RESPONSE_HEADERS = {
    "content-type",
    "date",
    "request-id",
    "server",
    "x-request-id",
}


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def endpoint_for(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def native_tool_calls(body: Any) -> Any:
    try:
        return body["choices"][0]["message"].get("tool_calls")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def first_message(body: Any) -> dict[str, Any]:
    try:
        message = body["choices"][0]["message"]
        return message if isinstance(message, dict) else {}
    except (KeyError, IndexError, TypeError):
        return {}


def validate_calls(calls: Any, expected_names: set[str]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not isinstance(calls, list) or not calls:
        return False, ["message.tool_calls is absent or empty"]
    for index, call in enumerate(calls):
        prefix = f"tool_calls[{index}]"
        if not isinstance(call, dict):
            problems.append(f"{prefix} is not an object")
            continue
        if call.get("type") != "function":
            problems.append(f"{prefix}.type is not 'function'")
        if not call.get("id"):
            problems.append(f"{prefix}.id is absent")
        function = call.get("function")
        if not isinstance(function, dict):
            problems.append(f"{prefix}.function is not an object")
            continue
        if function.get("name") not in expected_names:
            problems.append(
                f"{prefix}.function.name={function.get('name')!r}, "
                f"expected one of {sorted(expected_names)!r}"
            )
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            problems.append(f"{prefix}.function.arguments is not a JSON string")
        else:
            try:
                json.loads(arguments)
            except json.JSONDecodeError as exc:
                problems.append(f"{prefix}.function.arguments is invalid JSON: {exc}")
    return not problems, problems


def sdk_parse(body: Any) -> dict[str, Any]:
    try:
        from openai.types.chat import ChatCompletion
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        parsed = ChatCompletion.model_validate(body)
        dumped = parsed.model_dump(mode="json", exclude_none=True)
        parsed_calls = parsed.choices[0].message.tool_calls if parsed.choices else None
        return {
            "available": True,
            "parse_ok": True,
            "message": dumped.get("choices", [{}])[0].get("message", {}),
            "tool_calls_present": bool(parsed_calls),
        }
    except Exception as exc:
        return {
            "available": True,
            "parse_ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def load_json_sequence(path: Path) -> list[Any]:
    """Load one JSON value, a JSON array, or concatenated JSON objects."""
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        value, index = decoder.raw_decode(text, index)
        values.append(value)
    if len(values) == 1 and isinstance(values[0], list):
        return values[0]
    return values


def classify(
    *,
    case_name: str,
    status: int,
    body: Any,
    sdk_result: dict[str, Any],
    expected_name: str,
    allowed_names: set[str] | None = None,
) -> dict[str, Any]:
    message = first_message(body)
    calls = native_tool_calls(body)
    expected_names = allowed_names or {expected_name}
    valid_calls, call_problems = validate_calls(calls, expected_names)
    content = message.get("content")
    content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    text_looks_like_tool = any(
        marker in (content_text or "")
        for marker in (
            "<execute_bash",
            "<run_shell_cmd",
            "<run_shell",
            "<tool_call",
            "<function",
            expected_name,
        )
    )
    try:
        finish_reason = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        finish_reason = None

    if status < 200 or status >= 300:
        verdict = "HTTP_ERROR"
    elif valid_calls and not sdk_result.get("available"):
        verdict = "RAW_NATIVE_TOOL_CALL_OK_SDK_UNAVAILABLE"
    elif valid_calls and sdk_result.get("tool_calls_present"):
        verdict = "NATIVE_TOOL_CALL_OK"
    elif valid_calls and sdk_result.get("available") and not sdk_result.get("tool_calls_present"):
        verdict = "SDK_PARSE_OR_SCHEMA_MISMATCH"
    elif case_name == "forced" and text_looks_like_tool:
        verdict = "GATEWAY_RETURNED_TEXT_INSTEAD_OF_NATIVE_TOOL_CALL"
    elif case_name == "forced":
        verdict = "FORCED_TOOL_CHOICE_NOT_HONORED"
    else:
        verdict = "NO_NATIVE_TOOL_CALL"

    return {
        "verdict": verdict,
        "http_status": status,
        "finish_reason": finish_reason,
        "raw_tool_calls_present": bool(calls),
        "raw_tool_calls_valid": valid_calls,
        "raw_tool_call_problems": call_problems,
        "content_present": content is not None,
        "content_looks_like_text_tool_call": text_looks_like_tool,
        "usage_keys": sorted(body.get("usage", {}).keys()) if isinstance(body, dict) else [],
        "sdk": sdk_result,
    }


def request_once(
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "chainswe-tool-call-probe/1.0",
        },
        method="POST",
    )
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            status = response.status
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in SAFE_RESPONSE_HEADERS
            }
            return status, headers, response.read()
    except urllib.error.HTTPError as exc:
        headers = {
            key.lower(): value
            for key, value in exc.headers.items()
            if key.lower() in SAFE_RESPONSE_HEADERS
        }
        return exc.code, headers, exc.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--model")
    parser.add_argument("--endpoint", help="Override the derived /chat/completions URL")
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=("baseline", "auto", "forced"),
        default=("baseline", "auto", "forced"),
    )
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--tools-file", type=Path)
    parser.add_argument("--tool-name", default="diagnostic_echo")
    parser.add_argument("--max-tokens", type=int, default=256)
    args = parser.parse_args()

    env = os.environ.copy()
    env.update(load_dotenv(args.env_file.resolve()))
    api_key = env.get("OPENAI_API_KEY")
    base_url = env.get("OPENAI_BASE_URL")
    model = args.model or env.get("MODEL")
    missing = [name for name, value in {
        "OPENAI_API_KEY": api_key,
        "OPENAI_BASE_URL": base_url,
        "MODEL": model,
    }.items() if not value]
    if missing:
        parser.error(f"Missing configuration: {missing}")

    assert api_key is not None and base_url is not None and model is not None
    endpoint = args.endpoint or endpoint_for(base_url)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        output.chmod(0o700)
    except OSError:
        pass

    tool_name = args.tool_name
    if args.tools_file:
        tools = load_json_sequence(args.tools_file.resolve())
        available_names = [
            tool.get("function", {}).get("name")
            for tool in tools
            if isinstance(tool, dict)
        ]
        if tool_name not in available_names:
            parser.error(f"--tool-name {tool_name!r} not found in --tools-file: {available_names}")
    else:
        tools = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "Return the supplied diagnostic text. This is a protocol probe.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }]
    if args.prompt_file:
        prompt = args.prompt_file.resolve().read_text(encoding="utf-8")
    else:
        prompt = (
            f"Call the {tool_name} function with text='gateway-probe'. "
            "Do not answer in plain text."
        )
    common = {
        "model": model,
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    all_cases = {
        "baseline": dict(common),
        "auto": {**common, "tools": tools, "tool_choice": "auto"},
        "forced": {
            **common,
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        },
    }
    cases = {name: all_cases[name] for name in dict.fromkeys(args.cases)}

    summary: dict[str, Any] = {
        "schema_version": 1,
        "created_at": time.time(),
        "endpoint": endpoint,
        "model": model,
        "api_key_logged": False,
        "cases": {},
    }
    for case_name, payload in cases.items():
        case_dir = output / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "request.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            status, headers, raw = request_once(
                endpoint=endpoint,
                api_key=api_key,
                payload=payload,
                timeout=args.timeout,
            )
            (case_dir / "response.raw.json").write_bytes(raw)
            (case_dir / "response.headers.json").write_text(
                json.dumps(headers, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            try:
                body = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                result = {
                    "verdict": "INVALID_JSON_RESPONSE",
                    "http_status": status,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                sdk_result = sdk_parse(body)
                (case_dir / "sdk_parse.json").write_text(
                    json.dumps(sdk_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                result = classify(
                    case_name=case_name,
                    status=status,
                    body=body,
                    sdk_result=sdk_result,
                    expected_name=tool_name,
                    allowed_names=set(available_names) if case_name == "auto" else None,
                )
        except Exception as exc:
            result = {
                "verdict": "REQUEST_EXCEPTION",
                "error": f"{type(exc).__name__}: {exc}",
            }

        summary["cases"][case_name] = result
        (case_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"{case_name}: {result['verdict']}", flush=True)

    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
