#!/usr/bin/env python3
"""Aggregate the 30-chain run using the paper prefix-censor definition."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rate(num: int, den: int) -> float:
    return round(num / den, 6) if den else 0.0


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((run_dir / "chain_runs").glob("*/chains/*/*/chain_result.json")):
        chain = load(path)
        for stage in chain.get("stages", []):
            item = dict(stage)
            item.update({"continuous_id": chain["continuous_id"], "repo": chain["repo"], "mode": chain["mode"], "chain_complete": bool(chain.get("complete")), "chain_result_path": str(path)})
            item["actual_resolved"] = bool(item.get("resolved")) and item.get("failure_type") != "INFRA_ERROR"
            rows.append(item)
    if not rows:
        raise SystemExit(f"No chain results found under {run_dir}")
    return rows


def score(rows: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["continuous_id"], row["mode"])].append(row)
    for (chain, mode), values in grouped.items():
        failed = False
        for row in sorted(values, key=lambda value: int(value["position"])):
            row["paper_resolved"] = bool(row["actual_resolved"] and (mode == "oracle" or not failed))
            row["paper_prefix_censored"] = bool(mode != "oracle" and failed)
            if not row["actual_resolved"]:
                failed = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    out = args.out_dir or args.run_dir / "aggregate"
    rows = read_rows(args.run_dir)
    score(rows)
    write_csv(out / "stage_records_30.csv", rows)

    by_mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_mode[row["mode"]].append(row)
    mode_summary = []
    position_summary = []
    for mode, values in sorted(by_mode.items()):
        by_chain: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in values:
            by_chain[row["continuous_id"]].append(row)
        mode_summary.append({
            "mode": mode, "chains": len(by_chain), "stages": len(values),
            "actual_resolved": sum(row["actual_resolved"] for row in values),
            "actual_resolution_rate": rate(sum(row["actual_resolved"] for row in values), len(values)),
            "paper_resolved": sum(row["paper_resolved"] for row in values),
            "per_bug_resolution": rate(sum(row["paper_resolved"] for row in values), len(values)),
            "chain_success": sum(all(row["actual_resolved"] for row in chain_rows) for chain_rows in by_chain.values()),
            "chain_success_rate": rate(sum(all(row["actual_resolved"] for row in chain_rows) for chain_rows in by_chain.values()), len(by_chain)),
        })
        for position in sorted({int(row["position"]) for row in values}):
            at_position = [row for row in values if int(row["position"]) == position]
            position_summary.append({
                "mode": mode, "position": position, "stages": len(at_position),
                "actual_resolved": sum(row["actual_resolved"] for row in at_position),
                "actual_resolution_rate": rate(sum(row["actual_resolved"] for row in at_position), len(at_position)),
                "paper_resolved": sum(row["paper_resolved"] for row in at_position),
                "paper_resolution_rate": rate(sum(row["paper_resolved"] for row in at_position), len(at_position)),
            })

    keyed = {(row["continuous_id"], int(row["position"]), row["mode"]): row for row in rows}
    chain_errors = []
    for (chain, position, mode), oracle in sorted(keyed.items()):
        if mode != "oracle" or position < 2:
            continue
        seq = keyed.get((chain, position, "seq"))
        seq_mem = keyed.get((chain, position, "seq_mem"))
        chain_errors.append({
            "continuous_id": chain, "repo": oracle["repo"], "position": position,
            "instance_id": oracle["instance_id"], "oracle_actual_resolved": oracle["actual_resolved"],
            "seq_actual_resolved": seq["actual_resolved"] if seq else None,
            "seq_mem_actual_resolved": seq_mem["actual_resolved"] if seq_mem else None,
            "seq_chain_error": bool(seq and oracle["actual_resolved"] and not seq["actual_resolved"]),
            "seq_mem_chain_error": bool(seq_mem and oracle["actual_resolved"] and not seq_mem["actual_resolved"]),
        })
    chain_error_summary = {}
    for mode in ("seq", "seq_mem"):
        errors = sum(row[f"{mode}_chain_error"] for row in chain_errors)
        failures = sum(row[f"{mode}_actual_resolved"] is False for row in chain_errors)
        chain_error_summary[f"{mode}_chain_errors"] = errors
        chain_error_summary[f"{mode}_raw_downstream_failures"] = failures
        chain_error_summary[f"{mode}_chain_error_rate"] = rate(errors, failures)

    failure_summary = []
    for mode, values in sorted(by_mode.items()):
        counts: dict[str, int] = defaultdict(int)
        for row in values:
            if not row["actual_resolved"]:
                counts[row.get("failure_type") or "UNKNOWN"] += 1
        failure_summary.extend({"mode": mode, "failure_type": failure, "count": count} for failure, count in sorted(counts.items()))
    summary = {"run_dir": str(args.run_dir.resolve()), "mode_summary": mode_summary, "position_summary": position_summary, "chain_error_summary": chain_error_summary, "failure_summary": failure_summary}
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary_0907_metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(out / "mode_summary.csv", mode_summary)
    write_csv(out / "position_summary_all.csv", position_summary)
    write_csv(out / "chain_errors.csv", chain_errors)
    write_csv(out / "failure_summary.csv", failure_summary)
    report = ["# 0907 XH 30-chain metrics", "", f"Run dir: `{args.run_dir}`", "", "| mode | chains | stages | raw actual | paper per-bug | chain success |", "|---|---:|---:|---:|---:|---:|"]
    for row in mode_summary:
        report.append(f"| {row['mode']} | {row['chains']} | {row['stages']} | {row['actual_resolved']}/{row['stages']} ({row['actual_resolution_rate']:.2%}) | {row['paper_resolved']}/{row['stages']} ({row['per_bug_resolution']:.2%}) | {row['chain_success']}/{row['chains']} ({row['chain_success_rate']:.2%}) |")
    report += ["", "Paper per-bug uses prefix-censor for `seq` and `seq_mem`; raw actual only checks the current stage."]
    (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
