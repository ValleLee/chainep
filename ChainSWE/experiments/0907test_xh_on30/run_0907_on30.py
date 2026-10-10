#!/usr/bin/env python3
"""Run the 30 strict-F2P/P2P ChainSWE chains with a selectable model config."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIST = Path(__file__).with_name("chain_list_30.txt")
DEFAULT_DATA = ROOT / "data.jsonl"
DEFAULT_MODELS = ROOT / "configs" / "models.json"
DEFAULT_OUT_ROOT = Path(__file__).with_name("runs")
DEFAULT_ENV_FILE = ROOT.parent / "SWE-Edit" / ".env"
RUN_EXPERIMENT = ROOT / "scripts" / "run_experiment.py"


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "item"


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_dotenv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("'").strip('"')
    return result


def read_chain_ids(path: Path) -> list[str]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            values = [row["continuous_id"].strip() for row in csv.DictReader(handle)]
    else:
        values = [line.strip().split()[0] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    values = list(dict.fromkeys(value for value in values if value and not value.startswith("#")))
    if not values:
        raise SystemExit(f"No chain IDs found in {path}")
    return values


def load_data(path: Path) -> dict[str, dict[str, Any]]:
    rows = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[str(row["continuous_id"])] = row
    return rows


def load_model(path: Path, name: str) -> dict[str, Any]:
    models = json.loads(path.read_text(encoding="utf-8")).get("models", [])
    matches = [model for model in models if model.get("name") == name]
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one model named {name!r}; available={[m.get('name') for m in models]}")
    return matches[0]


def run_logged(command: list[str], log_path: Path, env: dict[str, str]) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="surrogatepass") as log:
        log.write("\n$ " + " ".join(command) + "\n")
        log.flush()
        return subprocess.run(command, cwd=ROOT.parent, env=env, stdout=log, stderr=subprocess.STDOUT).returncode


def ensure_image(image: str, log_path: Path, attempts: int) -> None:
    check = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode == 0:
        return
    for attempt in range(1, attempts + 1):
        if run_logged(["docker", "pull", image], log_path, os.environ.copy()) == 0:
            return
        if attempt == attempts:
            break
    raise RuntimeError(f"Unable to pull Docker image: {image}")


def run_one(chain_id: str, row: dict[str, Any], run_dir: Path, args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    chain_dir = run_dir / "chain_runs" / safe_name(chain_id)
    log_path = run_dir / "logs" / f"{safe_name(chain_id)}.log"
    try:
        if args.dry_run:
            return {"chain": chain_id, "status": "planned", "chain_run_dir": str(chain_dir)}
        ensure_image(str(row["docker_image"]), log_path, args.pull_attempts)
        command = [
            sys.executable, str(RUN_EXPERIMENT),
            "--data", str(args.data),
            "--chain", chain_id,
            "--model-config", str(args.model_config),
            "--model", args.model,
            "--workers", "1",
            "--agent-type", args.agent_type,
            "--reasoning-effort", args.reasoning_effort,
            "--agent-timeout", str(args.agent_timeout),
            "--test-timeout", str(args.test_timeout),
            "--collect-timeout", str(args.collect_timeout),
            "--out", str(chain_dir),
            "--modes", *args.modes,
        ]
        if args.resume and (chain_dir / "run_config.json").is_file():
            command.append("--resume")
        rc = run_logged(command, log_path, env)
        complete = (chain_dir / "COMPLETED").is_file()
        return {"chain": chain_id, "status": "completed" if rc == 0 and complete else "failed", "returncode": rc, "chain_run_dir": str(chain_dir), "log": str(log_path)}
    except BaseException as exc:
        return {"chain": chain_id, "status": "failed", "error": f"{type(exc).__name__}: {exc}", "chain_run_dir": str(chain_dir), "log": str(log_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the 30 strict ChainSWE chains.")
    parser.add_argument("--chain-list", type=Path, default=DEFAULT_LIST)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--model-config", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--model", required=True)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--uv-bin",
        type=Path,
        help="Host uv executable to expose through PATH for the ChainSWE runner",
    )
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--modes", nargs="+", default=["oracle", "seq", "seq_mem"])
    parser.add_argument("--parallel-chains", type=int, default=20)
    parser.add_argument("--agent-type", default="baseline")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--agent-timeout", type=int, default=1800)
    parser.add_argument("--test-timeout", type=int, default=1800)
    parser.add_argument("--collect-timeout", type=int, default=180)
    parser.add_argument("--pull-attempts", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.parallel_chains < 1:
        parser.error("--parallel-chains must be >= 1")
    for name in ("chain_list", "data", "model_config", "env_file", "out_root"):
        setattr(args, name, getattr(args, name).resolve())
    if args.uv_bin is not None:
        args.uv_bin = args.uv_bin.resolve()
        if not args.uv_bin.is_file() or not os.access(args.uv_bin, os.X_OK):
            parser.error(f"--uv-bin is not an executable file: {args.uv_bin}")
    ids = read_chain_ids(args.chain_list)
    data = load_data(args.data)
    missing = sorted(set(ids) - set(data))
    if missing:
        parser.error(f"IDs missing from data.jsonl: {missing}")
    model = load_model(args.model_config, args.model)
    env = os.environ.copy()
    env.update(load_dotenv(args.env_file))
    if args.uv_bin is not None:
        env["PATH"] = str(args.uv_bin.parent) + os.pathsep + env.get("PATH", "")
    missing_env = [source for source in model.get("env", {}).values() if not env.get(source)]
    if missing_env:
        parser.error(f"Missing model environment variables: {missing_env}")

    run_dir = args.out_root / args.run_name
    existing_run_files = (
        [path for path in run_dir.iterdir() if path.name not in {"launcher.log", "summarize.log"}]
        if run_dir.exists()
        else []
    )
    if existing_run_files and not args.resume and not args.dry_run:
        parser.error(f"Run already exists: {run_dir}; choose another --run-name or use --resume")
    run_dir.mkdir(parents=True, exist_ok=True)
    dump_json(run_dir / "launcher_config.json", {
        "chain_count": len(ids), "chain_list": str(args.chain_list.resolve()), "data": str(args.data.resolve()),
        "model_config": str(args.model_config.resolve()), "model": args.model, "model_snapshot": model,
        "modes": args.modes, "parallel_chains": args.parallel_chains, "agent_type": args.agent_type,
        "reasoning_effort": args.reasoning_effort, "agent_timeout": args.agent_timeout,
        "test_timeout": args.test_timeout, "collect_timeout": args.collect_timeout,
        "uv_bin": str(args.uv_bin) if args.uv_bin is not None else None,
    })
    dump_json(run_dir / "manifest_30.json", {"chains": [{"continuous_id": cid, "repo": data[cid]["repo"], "docker_image": data[cid]["docker_image"], "length": len(data[cid]["bug_fixes"])} for cid in ids]})

    selected = [(cid, data[cid]) for cid in ids]
    if args.dry_run:
        for cid, row in selected:
            print(f"DRY_TASK {args.model} {args.modes} {cid} stages={len(row['bug_fixes'])}")
        print(f"dry-run: {len(selected)} chains, {len(selected) * len(args.modes)} chain-mode tasks")
        return 0

    (run_dir / "RUNNING").touch()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel_chains) as pool:
        futures = [pool.submit(run_one, cid, row, run_dir, args, env) for cid, row in selected]
        outcomes = [future.result() for future in futures]
    dump_json(run_dir / "launcher_results.json", sorted(outcomes, key=lambda item: item["chain"]))
    all_ok = all(item["status"] == "completed" for item in outcomes)
    (run_dir / ("COMPLETED" if all_ok else "PARTIAL")).touch()
    opposite = run_dir / ("PARTIAL" if all_ok else "COMPLETED")
    if opposite.exists():
        opposite.unlink()
    (run_dir / "RUNNING").unlink(missing_ok=True)
    print(f"run {'complete' if all_ok else 'partial'}: {run_dir}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
