#!/usr/bin/env python3
"""Check that a copied ChainSWE/SWE-Edit tree can run the 30-chain experiment."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = Path(__file__).resolve().parent
EXPECTED_DATA_SHA256 = "5bdf3717a4f0d5e4f4f47152757e98ce706605427fc3669e18adaa0f02b40fad"
EXPECTED_WHEEL_SHA256 = "76c5387438c1a0f2254496975c6c5a1c02f7514116865afa3d0e41f401f7d499"


def sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'").strip('"')
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--uv-bin", type=Path, help="Host uv executable used by the launcher")
    parser.add_argument("--check-images", action="store_true")
    args = parser.parse_args()

    errors: list[str] = []
    required = {
        "experiment list": EXPERIMENT / "chain_list_30.txt",
        "launcher": EXPERIMENT / "run_0907_on30.py",
        "summarizer": EXPERIMENT / "summarize_0907_metrics.py",
        "dataset": ROOT / "data.jsonl",
        "runner": ROOT / "scripts" / "run_experiment.py",
        "runner package": ROOT / "scripts" / "chainswe_exp" / "runner.py",
        "agent server": ROOT / "scripts" / "chainswe_exp" / "agent_server.py",
        "SWE-Edit wheel": ROOT.parent / "SWE-Edit" / "dist" / "sweedit-1.0.0-py3-none-any.whl",
        "model config": args.model_config,
    }
    for label, path in required.items():
        if not path.is_file():
            errors.append(f"{label} missing: {path}")

    if (ROOT / "data.jsonl").is_file() and sha256(ROOT / "data.jsonl") != EXPECTED_DATA_SHA256:
        errors.append("data.jsonl SHA256 differs from the 0829 experiment")
    wheel = ROOT.parent / "SWE-Edit" / "dist" / "sweedit-1.0.0-py3-none-any.whl"
    if wheel.is_file() and sha256(wheel) != EXPECTED_WHEEL_SHA256:
        errors.append("SWE-Edit wheel SHA256 differs from the recorded wheel")

    model = None
    if args.model_config.is_file():
        models = json.loads(args.model_config.read_text(encoding="utf-8")).get("models", [])
        matches = [value for value in models if value.get("name") == args.model]
        if len(matches) != 1:
            errors.append(f"model {args.model!r} is not unique in {args.model_config}")
        else:
            model = matches[0]
            env = load_env(args.env_file)
            missing = [source for source in model.get("env", {}).values() if not env.get(source)]
            if missing:
                errors.append(f"model environment variables missing: {missing}")

    ids = []
    if (EXPERIMENT / "chain_list_30.txt").is_file():
        ids = [line.strip() for line in (EXPERIMENT / "chain_list_30.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(ids) != 30 or len(set(ids)) != 30:
            errors.append(f"chain_list_30.txt must contain 30 unique IDs; found {len(ids)}")

    data_rows = {}
    if (ROOT / "data.jsonl").is_file():
        for line in (ROOT / "data.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                data_rows[value["continuous_id"]] = value
        missing_ids = sorted(set(ids) - set(data_rows))
        if missing_ids:
            errors.append(f"chain IDs missing from data.jsonl: {missing_ids}")

    if shutil.which("docker") is None:
        errors.append("docker executable not found")
    else:
        docker = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if docker.returncode != 0:
            errors.append("Docker daemon is unavailable")
        if args.check_images:
            images = sorted({data_rows[chain]["docker_image"] for chain in ids if chain in data_rows})
            missing_images = []
            for image in images:
                result = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode != 0:
                    missing_images.append(image)
            if missing_images:
                errors.append(f"missing Docker images: {missing_images}")
    uv_path = args.uv_bin.resolve() if args.uv_bin is not None else None
    if uv_path is not None:
        if not uv_path.is_file() or not os.access(uv_path, os.X_OK):
            errors.append(f"uv executable is unavailable: {uv_path}")
    elif shutil.which("uv") is None:
        errors.append("uv executable not found; provide --uv-bin or add uv to PATH")

    result = {
        "ok": not errors,
        "root": str(ROOT),
        "experiment": str(EXPERIMENT),
        "model": model,
        "chain_count": len(ids),
        "uv_bin": str(uv_path) if uv_path is not None else shutil.which("uv"),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
