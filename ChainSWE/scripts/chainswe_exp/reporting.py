from __future__ import annotations

import csv
import json
import math
import random
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .common import json_dump_atomic, json_load


def load_run_stages(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((run_dir / "chains").glob("*/*/chain_result.json")):
        chain = json_load(path)
        for stage in chain.get("stages", []):
            item = dict(stage)
            item.update(
                {
                    "continuous_id": chain["continuous_id"],
                    "repo": chain["repo"],
                    "docker_image": chain["docker_image"],
                    "base_commit": chain["base_commit"],
                    "mode": chain["mode"],
                    "chain_complete": bool(chain.get("complete")),
                }
            )
            rows.append(item)
    rows.sort(key=lambda row: (row["continuous_id"], row["mode"], int(row["position"])))
    return rows


def add_paper_scoring(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["continuous_id"], row["mode"])].append(row)
    for (_, mode), stages in groups.items():
        if mode == "oracle":
            for stage in stages:
                actual = bool(stage.get("resolved")) and stage.get("failure_type") != "INFRA_ERROR"
                stage["actual_resolved"] = actual
                stage["paper_resolved"] = actual
                stage["paper_prefix_censored"] = False
            continue
        censored = False
        for stage in sorted(stages, key=lambda item: int(item["position"])):
            actual = bool(stage.get("resolved")) and stage.get("failure_type") != "INFRA_ERROR"
            stage["actual_resolved"] = actual
            stage["paper_resolved"] = bool(actual and not censored)
            stage["paper_prefix_censored"] = censored
            if not actual:
                censored = True


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def stage_flat(row: dict[str, Any]) -> dict[str, Any]:
    usage = row.get("usage") or {}
    tests = row.get("tests") or {}
    usage_unavailable = bool(
        row.get("model_name")
        and usage
        and usage.get("api_rounds", 0) == 0
        and usage.get("input_tokens", 0) == 0
        and usage.get("output_tokens", 0) == 0
        and usage.get("cost_usd") is None
    )
    return {
        "continuous_id": row["continuous_id"],
        "repo": row["repo"],
        "position": row["position"],
        "instance_id": row["instance_id"],
        "mode": row["mode"],
        "model_name": row.get("model_name"),
        "model_id": row.get("model_id"),
        "actual_resolved": row["actual_resolved"],
        "paper_resolved": row["paper_resolved"],
        "paper_prefix_censored": row["paper_prefix_censored"],
        "failure_type": row.get("failure_type"),
        "infra_error": row.get("infra_error"),
        "prior_unresolved": row.get("prior_unresolved", False),
        "recovered": row.get("recovered", False),
        "f2p_all_pass": tests.get("f2p_all_pass"),
        "p2p_all_pass": tests.get("p2p_all_pass"),
        "all_tests_collected": tests.get("all_tests_collected"),
        "usage_unavailable": usage_unavailable,
        "input_tokens": None if usage_unavailable else usage.get("input_tokens"),
        "cached_input_tokens": None if usage_unavailable else usage.get("cached_input_tokens"),
        "output_tokens": None if usage_unavailable else usage.get("output_tokens"),
        "reasoning_tokens": None if usage_unavailable else usage.get("reasoning_tokens"),
        "api_rounds": None if usage_unavailable else usage.get("api_rounds"),
        "peak_context_size": None if usage_unavailable else usage.get("peak_context_size"),
        "cost_usd": None if usage_unavailable else usage.get("cost_usd"),
        "seconds": row.get("seconds"),
        "model_patch_bytes": row.get("model_patch_bytes"),
        "chain_complete": row.get("chain_complete", False),
    }


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def sum_known(rows: Iterable[dict[str, Any]], field: str) -> float | None:
    values = [row.get(field) for row in rows if row.get(field) is not None]
    return round(sum(float(value) for value in values), 8) if values else None


def summarize(run_dir: Path) -> dict[str, Any]:
    raw = load_run_stages(run_dir)
    if not raw:
        raise ValueError(f"No chain results under {run_dir}")
    add_paper_scoring(raw)
    rows = [stage_flat(row) for row in raw]
    aggregate = run_dir / "aggregate"
    aggregate.mkdir(exist_ok=True)
    fields = list(rows[0])
    write_csv(aggregate / "stages.csv", rows, fields)

    valid = [row for row in rows if row["failure_type"] != "INFRA_ERROR" and row["chain_complete"]]
    infra = [row for row in rows if row["failure_type"] == "INFRA_ERROR" or not row["chain_complete"]]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        groups[row["mode"]].append(row)

    mode_summary = []
    for mode, stages in sorted(groups.items()):
        chains = defaultdict(list)
        for stage in stages:
            chains[stage["continuous_id"]].append(stage)
        actual = sum(bool(stage["actual_resolved"]) for stage in stages)
        paper = sum(bool(stage["paper_resolved"]) for stage in stages)
        chain_successes = sum(all(s["actual_resolved"] for s in values) for values in chains.values())
        opportunities = [stage for stage in stages if stage["prior_unresolved"]]
        recovered = sum(bool(stage["recovered"]) for stage in opportunities)
        mode_summary.append(
            {
                "mode": mode,
                "stages": len(stages),
                "chains": len(chains),
                "actual_resolved": actual,
                "actual_resolution_rate": rate(actual, len(stages)),
                "paper_resolved": paper,
                "paper_resolution_rate": rate(paper, len(stages)),
                "full_chains_resolved": chain_successes,
                "full_chain_resolution_rate": rate(chain_successes, len(chains)),
                "recovery_opportunities": len(opportunities),
                "recovered": recovered,
                "recovery_rate": rate(recovered, len(opportunities)),
                "usage_unavailable_stages": sum(bool(stage.get("usage_unavailable")) for stage in stages),
                "input_tokens": sum_known(stages, "input_tokens"),
                "output_tokens": sum_known(stages, "output_tokens"),
                "cost_usd": sum_known(stages, "cost_usd"),
            }
        )
    write_csv(aggregate / "mode_summary.csv", mode_summary, list(mode_summary[0]))

    position_groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        position_groups[(row["mode"], int(row["position"]))].append(row)
    position_summary = []
    for (mode, position), stages in sorted(position_groups.items()):
        actual = sum(bool(stage["actual_resolved"]) for stage in stages)
        paper = sum(bool(stage["paper_resolved"]) for stage in stages)
        opportunities = [stage for stage in stages if stage["prior_unresolved"]]
        position_summary.append(
            {
                "mode": mode,
                "position": position,
                "stages": len(stages),
                "actual_resolved": actual,
                "actual_resolution_rate": rate(actual, len(stages)),
                "paper_resolved": paper,
                "paper_resolution_rate": rate(paper, len(stages)),
                "recovery_opportunities": len(opportunities),
                "recovered": sum(bool(stage["recovered"]) for stage in opportunities),
                "recovery_rate": rate(sum(bool(stage["recovered"]) for stage in opportunities), len(opportunities)),
            }
        )
    write_csv(aggregate / "position_summary.csv", position_summary, list(position_summary[0]))

    keyed = {(row["continuous_id"], int(row["position"]), row["mode"]): row for row in valid}
    chain_errors = []
    for (chain_id, position, mode), oracle in sorted(keyed.items()):
        if mode != "oracle" or position <= 1:
            continue
        seq = keyed.get((chain_id, position, "seq"))
        seq_mem = keyed.get((chain_id, position, "seq_mem"))
        chain_errors.append(
            {
                "continuous_id": chain_id,
                "position": position,
                "instance_id": oracle["instance_id"],
                "oracle_actual_resolved": oracle["actual_resolved"],
                "seq_actual_resolved": seq["actual_resolved"] if seq else None,
                "seq_mem_actual_resolved": seq_mem["actual_resolved"] if seq_mem else None,
                "seq_chain_error": bool(seq is not None and oracle["actual_resolved"] and not seq["actual_resolved"]),
                "seq_mem_chain_error": bool(
                    seq_mem is not None and oracle["actual_resolved"] and not seq_mem["actual_resolved"]
                ),
            }
        )
    if chain_errors:
        write_csv(aggregate / "chain_error.csv", chain_errors, list(chain_errors[0]))

    summary = {
        "schema_version": 1,
        "run_dir": str(run_dir.resolve()),
        "raw_stages": len(rows),
        "valid_stages": len(valid),
        "infra_error_stages": len(infra),
        "mode_summary": mode_summary,
        "position_summary": position_summary,
        "chain_error": {
            "downstream_pairs": len(chain_errors),
            "seq_count": sum(bool(row["seq_chain_error"]) for row in chain_errors),
            "seq_mem_count": sum(bool(row["seq_mem_chain_error"]) for row in chain_errors),
            "seq_downstream_failures": sum(row["seq_actual_resolved"] is False for row in chain_errors),
            "seq_mem_downstream_failures": sum(row["seq_mem_actual_resolved"] is False for row in chain_errors),
            "seq_oracle_solvable": sum(
                bool(row["oracle_actual_resolved"]) and row["seq_actual_resolved"] is not None for row in chain_errors
            ),
            "seq_mem_oracle_solvable": sum(
                bool(row["oracle_actual_resolved"]) and row["seq_mem_actual_resolved"] is not None
                for row in chain_errors
            ),
        },
    }
    json_dump_atomic(aggregate / "summary.json", summary)
    return summary


def percentile(values: list[float], probability: float) -> float:
    values = sorted(values)
    if not values:
        return math.nan
    index = (len(values) - 1) * probability
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return values[low]
    return values[low] * (high - index) + values[high] * (index - low)


def cluster_bootstrap(
    rows: list[dict[str, Any]],
    value_field: str,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    by_chain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_chain[row["continuous_id"]].append(row)
    chains = sorted(by_chain)
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        sampled = [rng.choice(chains) for _ in chains]
        values = [bool(stage[value_field]) for chain in sampled for stage in by_chain[chain]]
        estimates.append(sum(values) / len(values))
    observed = sum(bool(row[value_field]) for row in rows) / len(rows)
    return {
        "estimate": round(observed, 6),
        "ci95_low": round(percentile(estimates, 0.025), 6),
        "ci95_high": round(percentile(estimates, 0.975), 6),
        "chains": len(chains),
        "stages": len(rows),
    }


def analyze(run_dir: Path, samples: int, seed: int) -> dict[str, Any]:
    raw = load_run_stages(run_dir)
    add_paper_scoring(raw)
    rows = [stage_flat(row) for row in raw if row.get("failure_type") != "INFRA_ERROR" and row.get("chain_complete")]
    by_mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_mode[row["mode"]].append(row)
    result = {"bootstrap_samples": samples, "bootstrap_seed": seed, "modes": {}}
    for index, (mode, stages) in enumerate(sorted(by_mode.items())):
        result["modes"][mode] = {
            "actual": cluster_bootstrap(stages, "actual_resolved", samples, seed + index * 2),
            "paper": cluster_bootstrap(stages, "paper_resolved", samples, seed + index * 2 + 1),
        }
    json_dump_atomic(run_dir / "aggregate" / "analysis.json", result)
    return result
