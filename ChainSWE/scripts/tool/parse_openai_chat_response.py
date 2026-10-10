#!/usr/bin/env python3
"""Parse a saved raw Chat Completions response with the OpenAI SDK."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from openai.types.chat import ChatCompletion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("response", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    body = json.loads(args.response.read_text(encoding="utf-8"))
    parsed = ChatCompletion.model_validate(body)
    message = parsed.choices[0].message
    calls = message.tool_calls or []
    result = {
        "parse_ok": True,
        "response_id": parsed.id,
        "finish_reason": parsed.choices[0].finish_reason,
        "content": message.content,
        "tool_call_count": len(calls),
        "tool_calls": [call.model_dump(mode="json", exclude_none=True) for call in calls],
        "usage": parsed.usage.model_dump(mode="json", exclude_none=True) if parsed.usage else None,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
