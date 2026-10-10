# ruff: noqa: E501
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def json_load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def json_dump_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o644)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Expected object at {path}:{number}")
            rows.append(value)
    return rows


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8", errors="surrogatepass"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(raw)


def safe_name(value: str, max_length: int = 80) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "item"
    return clean[:max_length]


def sorted_bugs(row: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(row["bug_fixes"], key=lambda bug: int(bug.get("order", 0)))


def validate_dataset(rows: list[dict[str, Any]]) -> None:
    chain_fields = {"continuous_id", "repo", "base_commit", "docker_image", "bug_fixes"}
    bug_fields = {
        "order",
        "swebench_instance_id",
        "problem_statement",
        "patch",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "test_cmds",
    }
    seen: set[str] = set()
    for index, row in enumerate(rows):
        missing = sorted(chain_fields - row.keys())
        if missing:
            raise ValueError(f"Chain row {index} missing fields: {missing}")
        chain_id = str(row["continuous_id"])
        if chain_id in seen:
            raise ValueError(f"Duplicate continuous_id: {chain_id}")
        seen.add(chain_id)
        bugs = sorted_bugs(row)
        if not bugs:
            raise ValueError(f"Chain has no bugs: {chain_id}")
        orders = [int(bug["order"]) for bug in bugs]
        if orders != list(range(1, len(bugs) + 1)):
            raise ValueError(f"Non-contiguous orders for {chain_id}: {orders}")
        for bug in bugs:
            missing = sorted(bug_fields - bug.keys())
            if missing:
                raise ValueError(f"Bug {bug.get('swebench_instance_id')} in {chain_id} missing fields: {missing}")


def select_rows(rows: list[dict[str, Any]], chain_ids: Iterable[str]) -> list[dict[str, Any]]:
    by_id = {str(row["continuous_id"]): row for row in rows}
    requested = list(dict.fromkeys(str(value) for value in chain_ids))
    missing = [value for value in requested if value not in by_id]
    if missing:
        raise ValueError(f"Unknown continuous_id(s): {', '.join(missing)}")
    return [by_id[value] for value in requested]


def render_prompt(problem_statement: str, repo_path: str) -> str:  # noqa: E501
    return f"""<uploaded_files>
{repo_path}
</uploaded_files>

I've uploaded a python code repository in the directory {repo_path} (not in /tmp/inputs). Consider the following issue descriptions:

<issue_description>
{problem_statement}
</issue_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <issue_description> are met?

I've already taken care of all changes to any of the test files described in the <issue_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!

Also the development Python environment is already set up for you (i.e., all dependencies already installed), so you don't need to install other packages.

Your task is to make the minimal changes to non-test files in the {repo_path} directory to ensure the <issue_description> is satisfied.

Follow these steps to resolve the issue:

1. As a first step, it might be a good idea to explore the repo to familiarize yourself with its structure.

2. Create a script to reproduce the error and execute it with `python <filename.py>` using the run_shell tool to confirm the error

- **Important:** If testing a Python package, add `import sys; sys.path.insert(0,'{repo_path}')` at the top of your script before package imports to ensure you're testing the local version, not an installed version.

3. Edit the source code of the repo to resolve the issue

4. Rerun your reproduce script and confirm that the error is fixed!

5. Think about edge cases and make sure your fix handles them as well.

Your thinking should be thorough and so it's fine if it's very long."""


def token_delta(after: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section, values in after.items():
        if not isinstance(values, dict):
            continue
        previous = before.get(section, {}) if isinstance(before.get(section), dict) else {}
        result[section] = {
            key: value - previous.get(key, 0) if isinstance(value, (int, float)) else value
            for key, value in values.items()
        }
    return result


def flatten_usage(stats: dict[str, Any]) -> dict[str, int]:
    agent = stats.get("agent", {})
    editor = stats.get("llm_editor", {})
    viewer = stats.get("llm_viewer", {})
    noncached = int(agent.get("total_noncached_input_tokens", 0) or 0)
    cached = int(agent.get("total_cached_input_tokens", 0) or 0)
    editor_in = int(editor.get("total_input_tokens", 0) or 0)
    viewer_in = int(viewer.get("total_input_tokens", 0) or 0)
    output = int(agent.get("total_output_tokens", 0) or 0)
    editor_out = int(editor.get("total_output_tokens", 0) or 0)
    viewer_out = int(viewer.get("total_output_tokens", 0) or 0)
    return {
        "input_tokens": noncached + cached + editor_in + viewer_in,
        "noncached_input_tokens": noncached + editor_in + viewer_in,
        "cached_input_tokens": cached,
        "output_tokens": output + editor_out + viewer_out,
        "reasoning_tokens": int(agent.get("total_reasoning_tokens", 0) or 0),
        "api_rounds": int(agent.get("num_rounds", 0) or 0)
        + int(editor.get("num_calls", 0) or 0)
        + int(viewer.get("num_calls", 0) or 0),
        "peak_context_size": int(agent.get("peak_context_size", 0) or 0),
    }


def compute_cost(usage: dict[str, int], model: dict[str, Any]) -> float | None:
    pricing = model.get("pricing_usd_per_million")
    if not isinstance(pricing, dict):
        return None
    if pricing.get("input") is None or pricing.get("output") is None:
        return None
    cached_rate = pricing.get("cached_input")
    if cached_rate is None:
        cached_rate = pricing["input"]
    cost = (
        usage["noncached_input_tokens"] * float(pricing["input"])
        + usage["cached_input_tokens"] * float(cached_rate)
        + usage["output_tokens"] * float(pricing["output"])
    ) / 1_000_000
    return round(cost, 8)
