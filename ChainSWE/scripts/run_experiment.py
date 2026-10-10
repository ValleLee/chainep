#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
from pathlib import Path

from chainswe_exp.common import (
    canonical_hash,
    json_dump_atomic,
    json_load,
    load_jsonl,
    safe_name,
    select_rows,
    sha256_file,
    sorted_bugs,
    validate_dataset,
)
from chainswe_exp.runner import VALID_MODES, public_model_snapshot, run_chain_mode

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "ChainSWE" / "data.jsonl"
DEFAULT_MODELS = ROOT / "ChainSWE" / "configs" / "models.json"
DEFAULT_WHEEL = ROOT / "SWE-Edit" / "dist" / "sweedit-1.0.0-py3-none-any.whl"
DEFAULT_AGENT_SERVER = Path(__file__).resolve().parent / "chainswe_exp" / "agent_server.py"


def load_model(path: Path, name: str):
    config = json_load(path)
    models = config.get("models", [])
    matches = [model for model in models if model.get("name") == name]
    if len(matches) != 1:
        available = [model.get("name") for model in models]
        raise ValueError(f"Expected one model named {name!r}; available={available}")
    return matches[0]


def load_selected_rows(data_path: Path, manifest_path: Path | None, chain_id: str | None):
    rows = load_jsonl(data_path)
    validate_dataset(rows)
    if chain_id:
        return select_rows(rows, [chain_id]), {"selection": "chain", "chains": [chain_id]}
    if manifest_path:
        manifest = json_load(manifest_path)
        if manifest.get("dataset_sha256") != sha256_file(data_path):
            raise ValueError("Manifest dataset SHA256 does not match --data")
        ids = [item["continuous_id"] if isinstance(item, dict) else item for item in manifest["chains"]]
        return select_rows(rows, ids), manifest
    raise ValueError("One of --manifest or --chain is required")


def expected_tasks(rows, modes):
    return [(row, mode) for row in rows for mode in modes]


def result_is_complete(path: Path, expected_stages: int) -> bool:
    try:
        value = json_load(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(value.get("complete")) and len(value.get("stages", [])) == expected_stages


def archive_incomplete(run_dir: Path, chain_id: str, mode: str) -> None:
    source = run_dir / "chains" / safe_name(chain_id) / mode
    if not source.exists():
        return
    root = run_dir / "failed_attempts" / safe_name(chain_id)
    root.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while (root / f"{mode}_attempt{attempt:02d}").exists():
        attempt += 1
    shutil.move(str(source), str(root / f"{mode}_attempt{attempt:02d}"))


def main():
    parser = argparse.ArgumentParser(description="Run ChainSWE stage-4/5 experiments")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--manifest", type=Path)
    selection.add_argument("--chain", help="Exact continuous_id (repo names are intentionally rejected)")
    parser.add_argument("--model-config", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--model", required=True, help="Model name from models.json")
    parser.add_argument("--modes", nargs="+", default=["oracle", "seq", "seq_mem"])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--agent-type", default="baseline", choices=["baseline", "baseline-v2", "llm-file-editor", "llm-editor"]
    )
    parser.add_argument("--reasoning-effort", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--agent-timeout", type=int, default=1800)
    parser.add_argument("--test-timeout", type=int, default=1800)
    parser.add_argument("--collect-timeout", type=int, default=180)
    parser.add_argument("--wheel", type=Path, default=DEFAULT_WHEEL)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    modes = list(dict.fromkeys(args.modes))
    invalid = sorted(set(modes) - VALID_MODES)
    if invalid:
        parser.error(f"Unknown modes: {invalid}")
    if args.workers < 1:
        parser.error("--workers must be >= 1")

    rows, manifest = load_selected_rows(args.data, args.manifest, args.chain)
    model = load_model(args.model_config, args.model)
    tasks = expected_tasks(rows, modes)
    stage_count = sum(len(sorted_bugs(row)) for row in rows) * len(modes)
    print(f"chains={len(rows)} modes={modes} chain_mode_tasks={len(tasks)} stages={stage_count}")
    for row, mode in tasks:
        print(f"DRY_TASK {model['name']} {mode} {row['continuous_id']} stages={len(row['bug_fixes'])}")
    if args.dry_run:
        return

    for required in [args.data, args.model_config, args.wheel, DEFAULT_AGENT_SERVER]:
        if not required.is_file():
            raise SystemExit(f"Required file is missing: {required}")

    run_config = {
        "schema_version": 1,
        "data_path": str(args.data.resolve()),
        "data_sha256": sha256_file(args.data),
        "manifest": manifest,
        "model": public_model_snapshot(model),
        "modes": modes,
        "workers": args.workers,
        "agent_type": args.agent_type,
        "reasoning_effort": args.reasoning_effort,
        "agent_timeout": args.agent_timeout,
        "test_timeout": args.test_timeout,
        "collect_timeout": args.collect_timeout,
        "wheel_sha256": sha256_file(args.wheel),
        "agent_server_sha256": sha256_file(DEFAULT_AGENT_SERVER),
    }
    # Worker count may change on resume without changing scientific semantics.
    identity = dict(run_config)
    identity.pop("workers")
    run_config["identity_sha256"] = canonical_hash(identity)

    args.out.mkdir(parents=True, exist_ok=True)
    config_path = args.out / "run_config.json"
    if config_path.exists():
        if not args.resume:
            raise SystemExit(f"Run already exists: {args.out}; use --resume")
        previous = json_load(config_path)
        if previous.get("identity_sha256") != run_config["identity_sha256"]:
            raise SystemExit("Refusing resume: frozen run configuration changed")
    else:
        if args.resume:
            raise SystemExit("--resume requested but run_config.json does not exist")
        json_dump_atomic(config_path, run_config)
    (args.out / "RUNNING").touch(exist_ok=True)

    remaining_by_chain = []
    for row in rows:
        remaining_modes = []
        for mode in modes:
            result_path = args.out / "chains" / safe_name(row["continuous_id"]) / mode / "chain_result.json"
            if args.resume and result_is_complete(result_path, len(row["bug_fixes"])):
                print(f"SKIP complete {row['continuous_id']} {mode}")
            else:
                if args.resume:
                    archive_incomplete(args.out, row["continuous_id"], mode)
                remaining_modes.append(mode)
        if remaining_modes:
            remaining_by_chain.append((row, remaining_modes))
    print(f"remaining_chains={len(remaining_by_chain)}")
    print(f"remaining_chain_mode_tasks={sum(len(item[1]) for item in remaining_by_chain)}")

    outcomes = []

    def run_one_chain(row, chain_modes):
        results = []
        for mode in chain_modes:
            out = args.out / "chains" / safe_name(row["continuous_id"]) / mode
            results.append(
                run_chain_mode(
                    row=row,
                    mode=mode,
                    model=model,
                    output_dir=out,
                    wheel=args.wheel,
                    agent_server=DEFAULT_AGENT_SERVER,
                    agent_type=args.agent_type,
                    reasoning_effort=args.reasoning_effort,
                    agent_timeout=args.agent_timeout,
                    test_timeout=args.test_timeout,
                    collect_timeout=args.collect_timeout,
                )
            )
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {
            pool.submit(run_one_chain, row, chain_modes): (row, chain_modes) for row, chain_modes in remaining_by_chain
        }

        for index, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            row, chain_modes = future_map[future]
            try:
                result = future.result()
            except BaseException as exc:
                result = [{"complete": False, "runner_exception": f"{type(exc).__name__}: {exc}"}]
            outcomes.extend(result)
            print(
                f"[{index}/{len(remaining_by_chain)}] {row['continuous_id']} "
                f"modes={chain_modes} complete={all(item.get('complete', False) for item in result)}",
                flush=True,
            )

    all_complete = True
    for row, mode in tasks:
        path = args.out / "chains" / safe_name(row["continuous_id"]) / mode / "chain_result.json"
        all_complete &= result_is_complete(path, len(row["bug_fixes"]))
    marker = args.out / ("COMPLETED" if all_complete else "PARTIAL")
    marker.touch()
    opposite = args.out / ("PARTIAL" if all_complete else "COMPLETED")
    if opposite.exists():
        opposite.unlink()
    running = args.out / "RUNNING"
    if running.exists():
        running.unlink()
    if not all_complete:
        raise SystemExit("Run is PARTIAL; fix infrastructure failures and use --resume")
    print(f"Run complete: {args.out}")


if __name__ == "__main__":
    main()
