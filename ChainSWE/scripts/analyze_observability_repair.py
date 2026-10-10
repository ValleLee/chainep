#!/usr/bin/env python3

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path


STATUS_RE = re.compile(
    r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(.+?)\s*$"
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


def normalize_node(node):
    node = (node or "").strip()
    node = node.replace("\\", "/")
    node = re.sub(r"^/testbed/", "", node)
    node = re.sub(r"^\./", "", node)
    return node


def strip_param_suffix(part):
    return re.sub(r"\[[^\]]*\]$", "", part)


def node_variants(node):
    node = normalize_node(node)
    variants = []

    def add(value, kind):
        value = normalize_node(value)
        if value:
            variants.append((kind, value))

    add(node, "exact")

    if "::" in node:
        parts = node.split("::")
        path = parts[0]
        tail = parts[1:]
        tail_no_params = tail[:-1] + [strip_param_suffix(tail[-1])]
        no_params = "::".join([path] + tail_no_params)
        add(no_params, "no_param")
        add(Path(path).name + "::" + "::".join(tail), "basename_exact")
        add(
            Path(path).name + "::" + "::".join(tail_no_params),
            "basename_no_param",
        )
        if len(tail) >= 2:
            add("::".join(tail), "class_func_exact")
            add("::".join(tail_no_params), "class_func_no_param")
        add(tail[-1], "last_exact")
        add(strip_param_suffix(tail[-1]), "last_no_param")
    elif "." in node:
        add(node.split(".")[-1], "dotted_last")

    out = []
    seen = set()
    for kind, value in variants:
        key = (kind, value)
        if key not in seen:
            seen.add(key)
            out.append((kind, value))
    return out


def parse_status_nodes(log_path):
    statuses = defaultdict(list)
    if not log_path.exists():
        return statuses
    text = log_path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        match = STATUS_RE.match(line)
        if not match:
            continue
        status, node = match.groups()
        node = normalize_node(node)
        if "::" not in node and ".py" not in node:
            continue
        statuses[node].append(status)
    return statuses


def build_index(statuses):
    index = defaultdict(list)
    for node, status_list in statuses.items():
        for kind, value in node_variants(node):
            index[(kind, value)].append((node, status_list))
    return index


def choose_mapping(test_id, statuses, index):
    declared = normalize_node(test_id)
    if declared in statuses:
        return {
            "mapped": declared,
            "method": "exact",
            "statuses": sorted(set(statuses[declared])),
            "ambiguous": False,
        }

    # Several ChainSWE/SWE-family rows contain pytest parameter ids that were
    # truncated at whitespace, e.g. "...::test_x[a == b" while pytest prints
    # "...::test_x[a == b or a == c]". Treat this as a repair only when the
    # truncated prefix points to exactly one observed node.
    if "[" in declared and "]" not in declared:
        prefix_matches = sorted(
            node
            for node in statuses
            if node.startswith(declared)
        )
        if len(prefix_matches) == 1:
            node = prefix_matches[0]
            return {
                "mapped": node,
                "method": "truncated_param_prefix",
                "statuses": sorted(set(statuses[node])),
                "ambiguous": False,
            }
        if len(prefix_matches) > 1:
            return {
                "mapped": None,
                "method": "truncated_param_prefix",
                "statuses": [],
                "ambiguous": True,
                "candidates": prefix_matches[:20],
            }

    methods = [
        "basename_exact",
        "no_param",
        "basename_no_param",
        "class_func_exact",
        "class_func_no_param",
        "last_exact",
        "last_no_param",
    ]

    for kind, value in node_variants(declared):
        if kind not in methods:
            continue
        candidates = index.get((kind, value), [])
        unique_nodes = sorted({node for node, _ in candidates})
        if len(unique_nodes) == 1:
            node = unique_nodes[0]
            return {
                "mapped": node,
                "method": kind,
                "statuses": sorted(set(statuses[node])),
                "ambiguous": False,
            }
        if len(unique_nodes) > 1:
            return {
                "mapped": None,
                "method": kind,
                "statuses": [],
                "ambiguous": True,
                "candidates": unique_nodes[:20],
            }

    return {
        "mapped": None,
        "method": "unresolved",
        "statuses": [],
        "ambiguous": False,
    }


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


def load_stage_rows(run_dir):
    with (run_dir / "stages.csv").open() as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--skip-chain", action="append", default=[])
    ap.add_argument("--base-valid-data")
    ap.add_argument("--candidate-data-out")
    args = ap.parse_args()

    data_path = Path(args.data)
    run_dir = Path(args.run_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    official_rows = load_jsonl(data_path)
    by_chain = {r["continuous_id"]: r for r in official_rows}
    run_chains = load_jsonl(run_dir / "chains.jsonl")
    stage_rows = load_stage_rows(run_dir)
    skip = set(args.skip_chain)

    stage_lookup = {
        (r["continuous_id"], int(r["order"])): r
        for r in stage_rows
        if r.get("order")
    }

    mapping_rows = []
    chain_reports = []
    repairable_ids = []
    counters = Counter()

    for chain in run_chains:
        cid = chain["continuous_id"]
        if cid in skip:
            continue
        if chain.get("chain_gold_valid"):
            continue

        row = by_chain[cid]
        chain_ok_with_id_mapping = True
        chain_ok_with_p2p_prune = True
        has_id_mapping_change = False
        has_p2p_prune = False
        chain_mapping_count = 0
        chain_prune_count = 0
        chain_unresolved = 0
        chain_ambiguous = 0
        chain_non_pass = 0
        stage_reports = []

        for stage in chain.get("stages", []):
            order = int(stage.get("order") or 0)
            st_csv = stage_lookup.get((cid, order), {})
            failure = stage.get("failure") or st_csv.get("failure") or ""
            blocked = bool(stage.get("blocked")) or st_csv.get("blocked") == "True"

            stage_report = {
                "order": order,
                "instance_id": stage.get("instance_id"),
                "failure": failure,
                "blocked": blocked,
                "repair": "already_valid" if stage.get("gold_valid") else "not_repairable",
                "mappings": [],
            }

            if stage.get("gold_valid"):
                stage_reports.append(stage_report)
                continue

            tests = stage.get("tests") or {}
            patch_ok = (
                (stage.get("test_patch") or {}).get("ok")
                and (stage.get("gold_patch") or {}).get("ok")
            )
            commands_ok = tests.get("commands_all_zero") is True
            missing = tests.get("missing_tests") or []

            if failure != "DECLARED_TEST_NOT_OBSERVABLE" or not patch_ok or not commands_ok:
                chain_ok_with_id_mapping = False
                counters[failure or "non_observability_failure"] += 1
                stage_reports.append(stage_report)
                continue

            log_path = (
                run_dir
                / "logs"
                / re.sub(r"[^A-Za-z0-9_.-]", "_", cid)
                / f"stage_{order:02d}_tests.log"
            )
            statuses = parse_status_nodes(log_path)
            index = build_index(statuses)

            stage_ok = True
            f2p_set = set(tests.get("f2p") or [])
            p2p_set = set(tests.get("p2p") or [])

            for tid in missing:
                mapping = choose_mapping(tid, statuses, index)
                status_set = set(mapping.get("statuses") or [])
                passed = status_set == {"PASSED"}
                changed = mapping.get("mapped") not in (None, normalize_node(tid))
                test_type = (
                    "F2P"
                    if tid in f2p_set
                    else ("P2P" if tid in p2p_set else "UNKNOWN")
                )
                record = {
                    "continuous_id": cid,
                    "repo": chain.get("repo"),
                    "order": order,
                    "instance_id": stage.get("instance_id"),
                    "declared_test_id": tid,
                    "test_type": test_type,
                    "mapped_test_id": mapping.get("mapped"),
                    "method": mapping.get("method"),
                    "statuses": sorted(status_set),
                    "ambiguous": mapping.get("ambiguous", False),
                    "passed": passed,
                    "p2p_prune_candidate": (
                        test_type == "P2P"
                        and not mapping.get("mapped")
                        and not mapping.get("ambiguous", False)
                    ),
                    "candidate_count": len(mapping.get("candidates") or []),
                }
                if mapping.get("candidates"):
                    record["candidates"] = mapping["candidates"]
                mapping_rows.append(record)
                stage_report["mappings"].append(record)

                if mapping.get("ambiguous"):
                    chain_ambiguous += 1
                    stage_ok = False
                elif not mapping.get("mapped"):
                    if test_type == "P2P":
                        chain_prune_count += 1
                        has_p2p_prune = True
                        chain_ok_with_id_mapping = False
                    else:
                        chain_unresolved += 1
                        stage_ok = False
                elif not passed:
                    chain_non_pass += 1
                    stage_ok = False
                else:
                    chain_mapping_count += 1
                    if changed:
                        has_id_mapping_change = True

            if stage_ok:
                stage_report["repair"] = "id_mapping"
            else:
                stage_report["repair"] = "not_repairable"
                chain_ok_with_id_mapping = False
                chain_ok_with_p2p_prune = False

            stage_reports.append(stage_report)

        report = {
            "continuous_id": cid,
            "repo": chain.get("repo"),
            "chain_ok_with_id_mapping": chain_ok_with_id_mapping,
            "chain_ok_with_p2p_prune": chain_ok_with_p2p_prune,
            "has_id_mapping_change": has_id_mapping_change,
            "has_p2p_prune": has_p2p_prune,
            "mapping_count": chain_mapping_count,
            "p2p_prune_count": chain_prune_count,
            "unresolved_count": chain_unresolved,
            "ambiguous_count": chain_ambiguous,
            "non_pass_count": chain_non_pass,
            "stages": stage_reports,
        }
        chain_reports.append(report)
        if chain_ok_with_id_mapping and has_id_mapping_change:
            repairable_ids.append(cid)

    p2p_prune_ids = [
        r["continuous_id"]
        for r in chain_reports
        if r["chain_ok_with_p2p_prune"] and r["has_p2p_prune"]
    ]

    write_jsonl(out_dir / "id_mapping_suggestions.jsonl", mapping_rows)
    write_jsonl(out_dir / "chain_repair_reports.jsonl", chain_reports)
    (out_dir / "id_repairable_chain_ids.txt").write_text(
        "\n".join(repairable_ids) + ("\n" if repairable_ids else ""),
        encoding="utf-8",
    )

    summary = {
        "run_dir": str(run_dir),
        "chains_analyzed": len(chain_reports),
        "chains_repairable_by_id_mapping": len(repairable_ids),
        "repairable_chain_ids": repairable_ids,
        "chains_repairable_by_p2p_prune": len(p2p_prune_ids),
        "p2p_prune_chain_ids": p2p_prune_ids,
        "mapping_rows": len(mapping_rows),
        "mappings_resolved_passed": sum(
            1 for r in mapping_rows if r["mapped_test_id"] and r["passed"]
        ),
        "mappings_unresolved": sum(
            1 for r in mapping_rows if not r["mapped_test_id"]
        ),
        "mappings_ambiguous": sum(1 for r in mapping_rows if r["ambiguous"]),
        "mappings_non_pass": sum(
            1 for r in mapping_rows if r["mapped_test_id"] and not r["passed"]
        ),
        "p2p_prune_candidate_rows": sum(
            1 for r in mapping_rows if r.get("p2p_prune_candidate")
        ),
        "non_id_failure_counts": dict(counters),
    }
    (out_dir / "observability_repair_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if args.candidate_data_out:
        candidate_ids = set(repairable_ids) | set(p2p_prune_ids)
        base_rows = (
            load_jsonl(args.base_valid_data)
            if args.base_valid_data
            else []
        )
        base_ids = {r["continuous_id"] for r in base_rows}
        rows_out = [deepcopy(r) for r in base_rows]
        mapping_by_key = {
            (
                r["continuous_id"],
                int(r["order"]),
                r["declared_test_id"],
            ): r["mapped_test_id"]
            for r in mapping_rows
            if r["continuous_id"] in candidate_ids
            and r["mapped_test_id"]
            and r["passed"]
        }
        prune_by_key = {
            (
                r["continuous_id"],
                int(r["order"]),
                r["declared_test_id"],
            )
            for r in mapping_rows
            if r["continuous_id"] in candidate_ids
            and r.get("p2p_prune_candidate")
        }

        for cid in sorted(candidate_ids):
            if cid in base_ids:
                continue
            repaired = deepcopy(by_chain[cid])
            for bug in repaired["bug_fixes"]:
                order = int(bug.get("order") or 0)
                for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
                    values = as_list(bug.get(field))
                    new_values = [
                        mapping_by_key.get((cid, order, v), v)
                        for v in values
                        if (cid, order, v) not in prune_by_key
                    ]
                    bug[field] = new_values
            rows_out.append(repaired)

        rows_out.sort(key=lambda r: r["continuous_id"])
        write_jsonl(Path(args.candidate_data_out), rows_out)

    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
