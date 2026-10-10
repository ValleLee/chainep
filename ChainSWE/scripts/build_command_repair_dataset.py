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
    m = re.search(r"\b(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+pytest|pytest)\b", command)
    if m:
        return command[: m.start()].rstrip()
    true_match = re.search(r"\b(?:true|:|/bin/true)\s*$", command.strip())
    if true_match:
        return command[: true_match.start()].rstrip()
    return ""


def make_pytest_command(original_cmd, test_ids):
    prefix = extract_prefix(original_cmd)
    tests = " ".join(shlex.quote(t) for t in test_ids)
    body = PYTEST_CMD
    if tests:
        body = body + " " + tests
    if prefix:
        return prefix + " " + body
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--official-data", required=True)
    ap.add_argument("--base-valid-data", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--parser-dir", required=True)
    ap.add_argument("--out-data", required=True)
    ap.add_argument("--manifest-out", required=True)
    ap.add_argument("--skip-chain", action="append", default=[])
    args = ap.parse_args()

    official = {r["continuous_id"]: r for r in load_jsonl(args.official_data)}
    base_rows = load_jsonl(args.base_valid_data)
    base_ids = {r["continuous_id"] for r in base_rows}
    run_chains = load_jsonl(Path(args.run_dir) / "chains.jsonl")
    skip = set(args.skip_chain)

    mappings = {}
    prune_candidates = set()
    for r in load_jsonl(Path(args.parser_dir) / "id_mapping_suggestions.jsonl"):
        key = (r["continuous_id"], int(r["order"]), r["declared_test_id"])
        if r.get("mapped_test_id") and r.get("passed"):
            mappings[key] = r["mapped_test_id"]
        if r.get("p2p_prune_candidate"):
            prune_candidates.add(key)

    candidate_rows = [deepcopy(r) for r in base_rows]
    candidate_ids = []
    repair_records = []

    for chain in run_chains:
        cid = chain["continuous_id"]
        if cid in skip or cid in base_ids or chain.get("chain_gold_valid"):
            continue

        stages = chain.get("stages") or []
        failures = {
            s.get("failure")
            for s in stages
            if s.get("failure")
        }
        blocked = [s for s in stages if s.get("blocked")]
        if failures - {"DECLARED_TEST_NOT_OBSERVABLE"} or blocked:
            continue

        repaired = deepcopy(official[cid])
        ok_candidate = True
        chain_record = {
            "continuous_id": cid,
            "strategy": "rewrite_test_cmds_for_missing_f2p_and_prune_unobservable_p2p",
            "stages": [],
        }

        stage_by_order = {
            int(s.get("order") or 0): s
            for s in stages
        }

        for bug in repaired["bug_fixes"]:
            order = int(bug.get("order") or 0)
            stage = stage_by_order.get(order)
            if not stage or stage.get("gold_valid"):
                continue

            tests = stage.get("tests") or {}
            missing = set(tests.get("missing_tests") or [])
            f2p = as_list(bug.get("FAIL_TO_PASS"))
            p2p = as_list(bug.get("PASS_TO_PASS"))

            missing_f2p = [t for t in f2p if t in missing]
            if not missing_f2p and stage.get("failure") != "DECLARED_TEST_NOT_OBSERVABLE":
                ok_candidate = False
                continue

            new_f2p = [
                mappings.get((cid, order, t), t)
                for t in f2p
            ]
            new_p2p = []
            pruned = 0
            remapped = 0
            for t in p2p:
                key = (cid, order, t)
                if key in prune_candidates or t in missing:
                    pruned += 1
                    continue
                mapped = mappings.get(key, t)
                if mapped != t:
                    remapped += 1
                new_p2p.append(mapped)

            observed_ids = []
            for t in new_f2p + new_p2p:
                if t not in observed_ids:
                    observed_ids.append(t)

            original_cmds = as_list(bug.get("test_cmds"))
            original_cmd = original_cmds[0] if original_cmds else ""
            if missing_f2p:
                bug["test_cmds"] = make_pytest_command(original_cmd, observed_ids)
            bug["FAIL_TO_PASS"] = new_f2p
            bug["PASS_TO_PASS"] = new_p2p

            chain_record["stages"].append(
                {
                    "order": order,
                    "instance_id": bug.get("swebench_instance_id"),
                    "missing_f2p": len(missing_f2p),
                    "p2p_pruned": pruned,
                    "test_id_remapped": remapped,
                    "rewrote_test_cmds": bool(missing_f2p),
                    "new_p2p_count": len(new_p2p),
                }
            )

        if ok_candidate and chain_record["stages"]:
            candidate_rows.append(repaired)
            candidate_ids.append(cid)
            repair_records.append(chain_record)

    candidate_rows.sort(key=lambda r: r["continuous_id"])
    write_jsonl(Path(args.out_data), candidate_rows)

    manifest = {
        "source_data": args.official_data,
        "base_valid_data": args.base_valid_data,
        "run_dir": args.run_dir,
        "parser_dir": args.parser_dir,
        "total_rows": len(candidate_rows),
        "candidate_added_count": len(candidate_ids),
        "candidate_chain_ids": candidate_ids,
        "skip_chains": sorted(skip),
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
