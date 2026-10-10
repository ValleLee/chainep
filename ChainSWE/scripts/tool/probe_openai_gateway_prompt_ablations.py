#!/usr/bin/env python3
"""Run forced-tool-call prompt ablations against an OpenAI-compatible gateway."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

from probe_openai_gateway_tool_calls import (
    classify,
    endpoint_for,
    load_dotenv,
    load_json_sequence,
    request_once,
    sdk_parse,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--tools-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tool-name", default="run_shell")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-tokens", type=int, default=512)
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

    exact = args.prompt_file.resolve().read_text(encoding="utf-8")
    without_wrappers = exact.replace(
        "<uploaded_files>\n/testbed\n</uploaded_files>",
        "Repository path: /testbed",
    ).replace("<issue_description>", "").replace("</issue_description>", "")
    without_angle_markup = re.sub(r"<[^>]*>", "", without_wrappers)
    native_instruction = (
        "Protocol requirement: use only the OpenAI native tool_calls field. "
        "Do not print XML-like tool tags in message content.\n\n" + exact
    )
    concise = (
        "The Python repository is in /testbed. Fix the WeasyPrint bug where text in an RTL "
        "table overlaps collapsed left and right borders. First inspect the repository by "
        "calling run_shell. Do not modify tests."
    )
    variants = {
        "exact": exact,
        "without_outer_wrappers": without_wrappers,
        "without_angle_markup": without_angle_markup,
        "native_protocol_instruction": native_instruction,
        "concise": concise,
    }

    tools = load_json_sequence(args.tools_file.resolve())
    endpoint = endpoint_for(base_url)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        output.chmod(0o700)
    except OSError:
        pass

    summary = {
        "schema_version": 1,
        "created_at": time.time(),
        "endpoint": endpoint,
        "model": model,
        "tool_name": args.tool_name,
        "api_key_logged": False,
        "variants": {},
    }
    for name, prompt in variants.items():
        variant_dir = output / name
        variant_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": model,
            "max_tokens": args.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "tools": tools,
            "tool_choice": {
                "type": "function",
                "function": {"name": args.tool_name},
            },
        }
        (variant_dir / "request.json").write_text(
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
            (variant_dir / "response.raw.json").write_bytes(raw)
            (variant_dir / "response.headers.json").write_text(
                json.dumps(headers, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            body = json.loads(raw)
            result = classify(
                case_name="forced",
                status=status,
                body=body,
                sdk_result=sdk_parse(body),
                expected_name=args.tool_name,
            )
        except Exception as exc:
            result = {
                "verdict": "PROBE_EXCEPTION",
                "error": f"{type(exc).__name__}: {exc}",
            }
        summary["variants"][name] = result
        (variant_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"{name}: {result['verdict']}", flush=True)

    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
