#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
from pathlib import Path

from chainswe_exp.common import (
    json_dump_atomic,
    load_jsonl,
    sha256_file,
    sorted_bugs,
    validate_dataset,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "ChainSWE" / "data.jsonl"


def chain_sources(row):
    return sorted({str(bug.get("source", "unknown")) for bug in sorted_bugs(row)})


def choose(rows, size, seed, includes, include_lengths):
    by_id = {row["continuous_id"]: row for row in rows}
    missing = [value for value in includes if value not in by_id]
    if missing:
        raise ValueError(f"Unknown --include values: {missing}")
    selected = [by_id[value] for value in includes]
    selected_ids = {row["continuous_id"] for row in selected}

    for length in include_lengths:
        if any(len(row["bug_fixes"]) == length for row in selected):
            continue
        candidates = [row for row in rows if len(row["bug_fixes"]) == length]
        if not candidates:
            raise ValueError(f"No chain with length {length}")
        candidates.sort(key=lambda row: row["continuous_id"])
        selected.append(candidates[0])
        selected_ids.add(candidates[0]["continuous_id"])

    if len(selected) > size:
        raise ValueError("Required chains exceed requested manifest size")

    rng = random.Random(seed)
    tie = {row["continuous_id"]: rng.random() for row in rows}
    while len(selected) < size:
        used_repos = {row["repo"] for row in selected}
        used_sources = {source for row in selected for source in chain_sources(row)}
        used_lengths = {len(row["bug_fixes"]) for row in selected}
        candidates = [row for row in rows if row["continuous_id"] not in selected_ids]
        if not candidates:
            break

        def score(
            row,
            used_sources=used_sources,
            used_repos=used_repos,
            used_lengths=used_lengths,
        ):
            sources = chain_sources(row)
            return (
                10 * sum(source not in used_sources for source in sources)
                + 5 * (row["repo"] not in used_repos)
                + 2 * (len(row["bug_fixes"]) not in used_lengths),
                tie[row["continuous_id"]],
            )

        winner = max(candidates, key=score)
        selected.append(winner)
        selected_ids.add(winner["continuous_id"])
    return selected


def main():
    parser = argparse.ArgumentParser(description="Create a frozen ChainSWE chain manifest")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--size", type=int)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--include-length", type=int, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.all == (args.size is not None):
        parser.error("Choose exactly one of --all or --size")

    rows = load_jsonl(args.data)
    validate_dataset(rows)
    selected = rows if args.all else choose(rows, args.size, args.seed, args.include, args.include_length)
    manifest = {
        "schema_version": 1,
        "dataset": str(args.data.resolve()),
        "dataset_sha256": sha256_file(args.data),
        "seed": args.seed,
        "selection": "all" if args.all else "deterministic_stratified_greedy",
        "chains": [
            {
                "continuous_id": row["continuous_id"],
                "repo": row["repo"],
                "length": len(row["bug_fixes"]),
                "sources": chain_sources(row),
            }
            for row in selected
        ],
    }
    json_dump_atomic(args.out, manifest)
    print(f"wrote {len(selected)} chains to {args.out}")
    print(f"dataset_sha256={manifest['dataset_sha256']}")


if __name__ == "__main__":
    main()
