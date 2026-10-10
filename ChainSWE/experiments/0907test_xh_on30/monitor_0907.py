#!/usr/bin/env python3
"""Live monitor for the 0907 30-chain run."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path


MODES = ("oracle", "seq", "seq_mem")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "item"


def mode_state(chain_dir: Path, chain_id: str, mode: str, expected: int) -> dict:
    mode_dir = chain_dir / "chains" / safe_name(chain_id) / mode
    result_path = mode_dir / "chain_result.json"
    stage_paths = sorted(mode_dir.glob("stages/*/result.json"))
    stage_dirs = sorted(path for path in (mode_dir / "stages").glob("*") if path.is_dir())
    stages = []
    complete = False
    if result_path.is_file():
        value = load(result_path)
        stages = value.get("stages", [])
        complete = bool(value.get("complete"))
    else:
        for path in stage_paths:
            try:
                stages.append(load(path))
            except (OSError, ValueError):
                pass
    infra = sum(stage.get("failure_type") == "INFRA_ERROR" for stage in stages)
    if complete:
        status = "complete"
    elif infra:
        status = "infra"
    elif stages:
        status = "partial"
    elif mode_dir.exists():
        status = "started"
    else:
        status = "pending"
    last = stages[-1] if stages else {}
    active = next((path.name for path in reversed(stage_dirs) if not (path / "result.json").is_file()), None)
    last_outcome = None
    if last:
        last_outcome = "RESOLVED" if last.get("resolved") else (last.get("failure_type") or "UNRESOLVED")
    return {
        "status": status,
        "done": len(stages),
        "expected": expected,
        "resolved": sum(bool(stage.get("resolved")) for stage in stages),
        "infra": infra,
        "active": active,
        "last_position": last.get("position"),
        "last_instance": last.get("instance_id"),
        "last_outcome": last_outcome,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor a 0907 XH 30-chain run.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--show", type=int, default=30)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    manifest = load(args.run_dir / "manifest_30.json")
    rows = []
    mode_counts = Counter()
    totals = Counter()
    for item in manifest["chains"]:
        chain_id = item["continuous_id"]
        expected = int(item.get("length", 3))
        chain_dir = args.run_dir / "chain_runs" / safe_name(chain_id)
        modes = {mode: mode_state(chain_dir, chain_id, mode, expected) for mode in MODES}
        for mode, state in modes.items():
            mode_counts[f"{mode}:{state['status']}"] += 1
            totals["done"] += state["done"]
            totals["expected"] += state["expected"]
            totals["resolved"] += state["resolved"]
            totals["infra"] += state["infra"]
        chain_status = "complete" if all(value["status"] == "complete" for value in modes.values()) else "running" if any(value["status"] != "pending" for value in modes.values()) else "pending"
        rows.append({"chain": chain_id, "status": chain_status, "modes": modes})

    chain_counts = Counter(row["status"] for row in rows)
    print(f"time={datetime.now().isoformat(timespec='seconds')}")
    print(f"run_dir={args.run_dir.resolve()}")
    print(f"markers RUNNING={(args.run_dir/'RUNNING').exists()} COMPLETED={(args.run_dir/'COMPLETED').exists()} PARTIAL={(args.run_dir/'PARTIAL').exists()}")
    print(f"chains total={len(rows)} complete={chain_counts['complete']} running={chain_counts['running']} pending={chain_counts['pending']}")
    print(f"stages done={totals['done']}/{totals['expected']} resolved={totals['resolved']} infra={totals['infra']}")
    print(f"mode_status={dict(sorted(mode_counts.items()))}")
    print("\nchain progress:")
    shown = rows if args.all else rows[: args.show]
    for row in shown:
        parts = []
        for mode in MODES:
            state = row["modes"][mode]
            details = []
            if state["active"]:
                details.append(f"active={state['active']}")
            if state["last_instance"]:
                details.append(
                    f"last={state['last_position']}:{state['last_instance']}:{state['last_outcome']}"
                )
            suffix = f"[{','.join(details)}]" if details else ""
            parts.append(f"{mode}:{state['status']}:{state['done']}/{state['expected']}{suffix}")
        print(f"{row['chain']} chain={row['status']} " + " ".join(parts))
    if len(rows) > len(shown):
        print(f"... {len(rows) - len(shown)} more chains hidden; use --all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
