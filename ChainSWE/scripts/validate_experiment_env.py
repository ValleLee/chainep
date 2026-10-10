#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from chainswe_exp.common import json_dump_atomic, json_load, load_jsonl, sha256_file, validate_dataset
from chainswe_exp.docker_runtime import inspect_image, run_command

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "ChainSWE" / "data.jsonl"
DEFAULT_MODELS = ROOT / "ChainSWE" / "configs" / "models.json"
DEFAULT_WHEEL = ROOT / "SWE-Edit" / "dist" / "sweedit-1.0.0-py3-none-any.whl"
EXPECTED_DATA_SHA256 = "5bdf3717a4f0d5e4f4f47152757e98ce706605427fc3669e18adaa0f02b40fad"
EXPECTED_SWEEDIT_COMMIT = "60ca6730714670a91bd6a48a53356be85e145248"


def model_by_name(path: Path, name: str):
    models = json_load(path).get("models", [])
    matches = [model for model in models if model.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Expected one model named {name!r}")
    return matches[0]


def git_head(path: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def main():
    parser = argparse.ArgumentParser(description="Read-only preflight for ChainSWE stage-4/5 runs")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--model", required=True)
    parser.add_argument("--wheel", type=Path, default=DEFAULT_WHEEL)
    parser.add_argument("--gold-summary", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    errors = []
    rows = load_jsonl(args.data)
    validate_dataset(rows)
    data_sha = sha256_file(args.data)
    if data_sha != EXPECTED_DATA_SHA256:
        errors.append("DATASET_SHA256_MISMATCH")

    manifest = json_load(args.manifest)
    if manifest.get("dataset_sha256") != data_sha:
        errors.append("MANIFEST_DATASET_SHA256_MISMATCH")
    selected_ids = [item["continuous_id"] if isinstance(item, dict) else item for item in manifest["chains"]]
    by_id = {row["continuous_id"]: row for row in rows}
    unknown = sorted(set(selected_ids) - by_id.keys())
    if unknown:
        errors.append("MANIFEST_UNKNOWN_CHAINS")
    selected = [by_id[value] for value in selected_ids if value in by_id]

    model = model_by_name(args.model_config, args.model)
    if not model.get("model_id") or "REPLACE_" in str(model.get("model_id")):
        errors.append("MODEL_ID_NOT_CONFIGURED")
    missing_env = [source for source in model.get("env", {}).values() if not os.environ.get(source)]
    if missing_env:
        errors.append("MODEL_ENV_MISSING")

    sweedit_head = git_head(ROOT / "SWE-Edit")
    if sweedit_head != EXPECTED_SWEEDIT_COMMIT:
        errors.append("SWEEDIT_COMMIT_MISMATCH")
    if not args.wheel.is_file():
        errors.append("SWEEDIT_WHEEL_MISSING")
    if not shutil.which("uv"):
        errors.append("UV_MISSING")

    docker_info = run_command(
        ["docker", "info", "--format", "Root={{.DockerRootDir}} Server={{.ServerVersion}}"],
        timeout=60,
    )
    images = {}
    if docker_info["rc"] != 0:
        errors.append("DOCKER_UNAVAILABLE")
    else:
        for row in selected:
            image = row["docker_image"]
            if image in images:
                continue
            try:
                images[image] = inspect_image(image)
            except RuntimeError as exc:
                images[image] = {"error": str(exc)}
                errors.append("DOCKER_IMAGE_MISSING")

    gold = None
    if args.gold_summary:
        if not args.gold_summary.is_file():
            errors.append("GOLD_SUMMARY_MISSING")
        else:
            gold = json_load(args.gold_summary)
            if gold.get("stages_gold_invalid", 0) or gold.get("missing_test_ids", 0):
                errors.append("GOLD_AUDIT_NOT_CLEAN")

    result = {
        "ok": not errors,
        "errors": sorted(set(errors)),
        "data": {"path": str(args.data.resolve()), "sha256": data_sha, "chains": len(rows)},
        "manifest": {
            "path": str(args.manifest.resolve()),
            "chains": len(selected),
            "unknown_chains": unknown,
        },
        "model": {
            "name": model.get("name"),
            "model_id": model.get("model_id"),
            "api_type": model.get("api_type"),
            "required_environment": {source: bool(os.environ.get(source)) for source in model.get("env", {}).values()},
        },
        "sweedit": {
            "commit": sweedit_head,
            "expected_commit": EXPECTED_SWEEDIT_COMMIT,
            "wheel": str(args.wheel.resolve()),
            "wheel_sha256": sha256_file(args.wheel) if args.wheel.is_file() else None,
        },
        "docker": {
            "available": docker_info["rc"] == 0,
            "info": docker_info["output"].strip() if docker_info["rc"] == 0 else docker_info["output"][-2000:],
            "images": images,
        },
        "gold_summary": gold,
    }
    json_dump_atomic(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
