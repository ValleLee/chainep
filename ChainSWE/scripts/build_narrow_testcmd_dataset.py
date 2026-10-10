#!/usr/bin/env python3

import argparse
import json
import re
import shlex
from copy import deepcopy
from pathlib import Path


PYTEST_CMD = (
    "pytest --no-header -rA --tb=line --color=no -p no:cacheprovider "
    "-W ignore::DeprecationWarning"
)


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except Exception:
            return [value]
        if isinstance(decoded, list):
            return [str(v) for v in decoded]
        return [value]
    return [str(value)]


def load_jsonl(path):
    rows = []
    with Path(path).open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def extract_prefix(command):
    command = command or ""
    match = re.search(
        r"\b(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+pytest|pytest)\b",
        command,
    )
    if match:
        return command[: match.start()].rstrip()
    true_match = re.search(r"\b(?:true|:|/bin/true)\s*$", command.strip())
    if true_match:
        return command[: true_match.start()].rstrip()
    return ""


def chunked_pytest_commands(prefix, test_ids, max_chars):
    commands = []
    current = []
    current_len = len(prefix) + len(PYTEST_CMD) + 2
    for tid in test_ids:
        quoted = shlex.quote(tid)
        extra = len(quoted) + 1
        if current and current_len + extra > max_chars:
            commands.append(make_command(prefix, current))
            current = []
            current_len = len(prefix) + len(PYTEST_CMD) + 2
        current.append(tid)
        current_len += extra
    if current:
        commands.append(make_command(prefix, current))
    return commands


def make_command(prefix, test_ids):
    tests = " ".join(shlex.quote(t) for t in test_ids)
    body = PYTEST_CMD + (" " + tests if tests else "")
    return (prefix + " " + body).strip() if prefix else body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-valid-data", required=True)
    ap.add_argument("--candidate-overlay", action="append", default=[])
    ap.add_argument("--chain-file", required=True)
    ap.add_argument("--out-data", required=True)
    ap.add_argument("--manifest-out", required=True)
    ap.add_argument("--max-command-chars", type=int, default=60000)
    args = ap.parse_args()

    rows = {r["continuous_id"]: r for r in load_jsonl(args.base_valid_data)}
    for overlay in args.candidate_overlay:
        for row in load_jsonl(overlay):
            rows[row["continuous_id"]] = row

    chain_ids = [
        line.strip()
        for line in Path(args.chain_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    repair_records = []
    for cid in chain_ids:
        if cid not in rows:
            continue
        row = deepcopy(rows[cid])
        changed_stages = []
        for bug in row.get("bug_fixes", []):
            tests = []
            for tid in as_list(bug.get("FAIL_TO_PASS")) + as_list(bug.get("PASS_TO_PASS")):
                if tid not in tests:
                    tests.append(tid)
            if not tests:
                continue
            original_cmds = as_list(bug.get("test_cmds"))
            prefix = extract_prefix(original_cmds[0] if original_cmds else "")
            new_cmds = chunked_pytest_commands(prefix, tests, args.max_command_chars)
            if new_cmds != original_cmds:
                bug["test_cmds"] = new_cmds if len(new_cmds) > 1 else new_cmds[0]
                changed_stages.append(
                    {
                        "order": bug.get("order"),
                        "instance_id": bug.get("swebench_instance_id"),
                        "test_count": len(tests),
                        "command_count": len(new_cmds),
                    }
                )
        if changed_stages:
            rows[cid] = row
            repair_records.append(
                {
                    "continuous_id": cid,
                    "strategy": "narrow_test_cmds_to_declared_f2p_p2p",
                    "stages": changed_stages,
                }
            )

    ordered = [rows[k] for k in sorted(rows)]
    write_jsonl(Path(args.out_data), ordered)
    manifest = {
        "base_valid_data": args.base_valid_data,
        "candidate_overlay": args.candidate_overlay,
        "chain_file": args.chain_file,
        "total_rows": len(ordered),
        "candidate_chain_count": len(chain_ids),
        "repaired_chain_count": len(repair_records),
        "repair_records": repair_records,
    }
    Path(args.manifest_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest_out).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
