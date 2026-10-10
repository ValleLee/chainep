#!/usr/bin/env python3

import argparse
import importlib.util
import json
import re
import shlex
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path


HERE = Path(__file__).resolve().parent
STRICT_PATH = HERE / "strict_gold_replay.py"
spec = importlib.util.spec_from_file_location("strict_gold_replay", STRICT_PATH)
strict = importlib.util.module_from_spec(spec)
spec.loader.exec_module(strict)


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


def load_chain_ids(path):
    return [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def git(container, shell, repo, command, timeout=120):
    return strict.dexec(
        container,
        shell,
        f"git -C {shlex.quote(repo)} {command}",
        timeout=timeout,
    )


def capture_diff(container, shell, repo):
    r = git(
        container,
        shell,
        repo,
        "diff --binary --no-ext-diff",
        timeout=120,
    )
    return r["output"] if r["rc"] == 0 else ""


def commit_all(container, shell, repo, message):
    git(container, shell, repo, "add -A", timeout=120)
    status = git(container, shell, repo, "status --porcelain", timeout=120)[
        "output"
    ]
    if not status.strip():
        return {"committed": False, "status": status, "rc": 0, "output": ""}
    r = git(
        container,
        shell,
        repo,
        (
            "-c user.email=chainswe-repair@example.invalid "
            "-c user.name=chainswe-repair commit -m "
            + shlex.quote(message)
        ),
        timeout=120,
    )
    return {
        "committed": r["rc"] == 0,
        "status": status,
        "rc": r["rc"],
        "output": r["output"][-4000:],
    }


def apply_first(container, repo, candidates):
    attempts = []
    for label, patch, threeway in candidates:
        result = strict.git_apply(container, repo, patch or "", threeway=threeway)
        attempts.append(
            {
                "label": label,
                "threeway": threeway,
                "ok": result["ok"],
                "empty": result["empty"],
                "check_rc": result["check_rc"],
                "apply_rc": result["apply_rc"],
                "check_output": result["check_output"][-2000:],
                "apply_output": result["apply_output"][-2000:],
            }
        )
        if result["ok"]:
            return label, result, attempts
    return None, None, attempts


def source_by_instance(source_rows):
    out = {}
    for row in source_rows:
        iid = row.get("instance_id")
        if iid and iid not in out:
            out[iid] = row
    return out


def materialize_chain(
    row,
    source_instances,
    out_dir,
    test_timeout,
    collect_timeout,
    keep_containers=False,
):
    chain_id = row["continuous_id"]
    image = row["docker_image"]
    base_commit = row["base_commit"]
    repo_name = row["repo"]
    safe_chain = safe_name(chain_id)
    log_dir = out_dir / "logs" / safe_chain
    log_dir.mkdir(parents=True, exist_ok=True)
    container = "chainswe-materialize-" + safe_chain[:40] + "-" + uuid.uuid4().hex[:8]
    result = {
        "continuous_id": chain_id,
        "repo": repo_name,
        "docker_image": image,
        "container": container,
        "started_at": time.time(),
        "ok": False,
        "stages": [],
    }
    repaired = deepcopy(row)

    start = strict.host_run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            "trap : TERM INT; while :; do sleep 3600; done",
        ],
        timeout=120,
    )
    result["container_start"] = {"rc": start["rc"], "output": start["output"]}
    if start["rc"] != 0:
        result["failure"] = "CONTAINER_START_FAILED"
        return result, None

    try:
        shell = strict.detect_shell(container)
        workdir = strict.image_workdir(image)
        repos = strict.discover_git_repos(container, shell, workdir)
        repo, base_exists, containing = strict.choose_repo(
            container, shell, repos, base_commit, repo_name
        )
        result.update(
            {
                "shell": shell,
                "image_workdir": workdir,
                "git_repo_candidates": repos,
                "repo_path": repo,
                "declared_base_commit": base_commit,
                "base_commit_exists": base_exists,
                "repos_containing_base": containing,
            }
        )
        if not repo:
            result["failure"] = "GIT_REPO_NOT_FOUND"
            return result, None

        baked_head = strict.get_head(container, shell, repo)
        result["baked_head"] = baked_head
        start_commit = base_commit if base_exists else baked_head
        result["start_commit"] = start_commit
        repaired["base_commit"] = start_commit

        if base_exists:
            reset = strict.reset_base(container, shell, repo, base_commit)
            result["base_reset"] = {"rc": reset["rc"], "output": reset["output"][-4000:]}
            if reset["rc"] != 0:
                result["failure"] = "BASE_RESET_FAILED"
                return result, None
        else:
            clean = git(container, shell, repo, "clean -fd", timeout=120)
            result["base_reset"] = {
                "rc": None,
                "output": "using baked HEAD because declared base is absent",
            }
            result["baked_clean"] = {"rc": clean["rc"], "output": clean["output"][-4000:]}

        git(container, shell, repo, "reset --hard HEAD", timeout=120)
        commit_all(container, shell, repo, "chainswe repair start")

        for bug in sorted(repaired["bug_fixes"], key=lambda b: b.get("order", 0)):
            order = int(bug.get("order") or 0)
            iid = bug.get("swebench_instance_id")
            source = source_instances.get(iid, {})
            stage = {"order": order, "instance_id": iid}

            test_candidates = [
                ("chain_test_patch_plain", bug.get("test_patch", ""), False),
                ("chain_test_patch_3way", bug.get("test_patch", ""), True),
            ]
            if source.get("test_patch") and source.get("test_patch") != bug.get("test_patch"):
                test_candidates.extend(
                    [
                        ("source_test_patch_plain", source.get("test_patch", ""), False),
                        ("source_test_patch_3way", source.get("test_patch", ""), True),
                    ]
                )

            label, applied, attempts = apply_first(container, repo, test_candidates)
            stage["test_patch_attempts"] = attempts
            if not applied:
                stage["failure"] = "TEST_PATCH_APPLY_FAILED"
                result["stages"].append(stage)
                result["failure"] = stage["failure"]
                return result, None
            test_diff = capture_diff(container, shell, repo)
            bug["test_patch"] = test_diff
            stage["test_patch_source"] = label
            stage["test_patch_diff_bytes"] = len(test_diff.encode("utf-8", errors="surrogatepass"))
            stage["test_commit"] = commit_all(container, shell, repo, f"stage {order} test patch")

            gold_candidates = [
                ("chain_gold_patch_plain", bug.get("patch", ""), False),
                ("chain_gold_patch_3way", bug.get("patch", ""), True),
            ]
            if source.get("patch") and source.get("patch") != bug.get("patch"):
                gold_candidates.extend(
                    [
                        ("source_gold_patch_plain", source.get("patch", ""), False),
                        ("source_gold_patch_3way", source.get("patch", ""), True),
                    ]
                )

            label, applied, attempts = apply_first(container, repo, gold_candidates)
            stage["gold_patch_attempts"] = attempts
            if not applied:
                stage["failure"] = "GOLD_PATCH_APPLY_FAILED"
                result["stages"].append(stage)
                result["failure"] = stage["failure"]
                return result, None
            gold_diff = capture_diff(container, shell, repo)
            bug["patch"] = gold_diff
            stage["gold_patch_source"] = label
            stage["gold_patch_diff_bytes"] = len(gold_diff.encode("utf-8", errors="surrogatepass"))
            stage["gold_commit"] = commit_all(container, shell, repo, f"stage {order} gold patch")

            test_log = log_dir / f"stage_{order:02d}_tests.log"
            tests = strict.run_tests(
                container,
                shell,
                repo,
                bug,
                test_log,
                timeout=test_timeout,
                collect_timeout=collect_timeout,
            )
            stage["tests"] = {
                "commands_all_zero": tests["commands_all_zero"],
                "all_tests_collected": tests["all_tests_collected"],
                "f2p_all_pass": tests["f2p_all_pass"],
                "p2p_all_pass": tests["p2p_all_pass"],
                "missing_test_count": len(tests["missing_tests"]),
                "command_results": tests["command_results"],
            }
            stage["gold_valid"] = (
                tests["commands_all_zero"]
                and tests["all_tests_collected"]
                and tests["f2p_all_pass"]
                and tests["p2p_all_pass"]
            )
            result["stages"].append(stage)
            if not stage["gold_valid"]:
                stage["failure"] = (
                    "DECLARED_TEST_NOT_OBSERVABLE"
                    if not tests["all_tests_collected"]
                    else (
                        "GOLD_TEST_COMMAND_FAILED"
                        if not tests["commands_all_zero"]
                        else "GOLD_VALIDATION_FAILED"
                    )
                )
                result["failure"] = stage["failure"]
                return result, None

        result["ok"] = True
        return result, repaired
    finally:
        if not keep_containers:
            strict.host_run(["docker", "rm", "-f", container], timeout=120)
        result["finished_at"] = time.time()
        result["seconds"] = round(result["finished_at"] - result["started_at"], 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--official-data", required=True)
    ap.add_argument("--overlay-data", action="append", default=[])
    ap.add_argument("--source-instances", required=True)
    ap.add_argument("--chain-file", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--test-timeout", type=int, default=900)
    ap.add_argument("--collect-timeout", type=int, default=180)
    ap.add_argument("--keep-containers", action="store_true")
    args = ap.parse_args()

    rows = {r["continuous_id"]: r for r in load_jsonl(args.official_data)}
    for overlay in args.overlay_data:
        for row in load_jsonl(overlay):
            rows[row["continuous_id"]] = row
    source = source_by_instance(load_jsonl(args.source_instances))
    chain_ids = load_chain_ids(args.chain_file)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    repaired_rows = []
    for i, cid in enumerate(chain_ids, 1):
        if cid not in rows:
            print(f"[{i}/{len(chain_ids)}] {cid} missing_from_data", flush=True)
            continue
        result, repaired = materialize_chain(
            rows[cid],
            source,
            out_dir,
            args.test_timeout,
            args.collect_timeout,
            keep_containers=args.keep_containers,
        )
        results.append(result)
        if repaired is not None:
            repaired_rows.append(repaired)
        write_jsonl(out_dir / "materialized_candidates.jsonl", repaired_rows)
        write_jsonl(out_dir / "materialization_results.jsonl", results)
        print(
            f"[{i}/{len(chain_ids)}] {cid} ok={result.get('ok')} failure={result.get('failure','')}",
            flush=True,
        )

    summary = {
        "chains_attempted": len(results),
        "chains_materialized_valid": sum(1 for r in results if r.get("ok")),
        "failure_counts": {},
    }
    for r in results:
        if not r.get("ok"):
            key = r.get("failure", "UNKNOWN")
            summary["failure_counts"][key] = summary["failure_counts"].get(key, 0) + 1
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
