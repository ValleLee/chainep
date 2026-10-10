#!/usr/bin/env python3

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path


ROOT = Path("/lg-home/cog8/lwli6/chain")
DATA = ROOT / "ChainSWE" / "data.jsonl"


def as_list(x):
    if x is None:
        return []

    if isinstance(x, list):
        return [str(v) for v in x]

    if isinstance(x, str):
        s = x.strip()

        if not s:
            return []

        try:
            obj = json.loads(s)
            if isinstance(obj, list):
                return [str(v) for v in obj]
        except Exception:
            pass

        return [x]

    return [str(x)]


def sha256_text(s):
    return hashlib.sha256(
        (s or "").encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def host_run(cmd, timeout=None, input_bytes=None):
    start = time.time()

    try:
        p = subprocess.run(
            cmd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )

        return {
            "rc": p.returncode,
            "timeout": False,
            "seconds": round(time.time() - start, 3),
            "output": p.stdout.decode(
                "utf-8",
                errors="replace",
            ),
        }

    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""

        if isinstance(out, str):
            text = out
        else:
            text = out.decode("utf-8", errors="replace")

        return {
            "rc": 124,
            "timeout": True,
            "seconds": round(time.time() - start, 3),
            "output": text,
        }


def dexec(container, shell, command, timeout=300):
    return host_run(
        [
            "docker",
            "exec",
            container,
            shell,
            "-lc",
            command,
        ],
        timeout=timeout,
    )


def image_workdir(image):
    r = host_run(
        [
            "docker",
            "image",
            "inspect",
            image,
            "--format",
            "{{.Config.WorkingDir}}",
        ],
        timeout=30,
    )

    if r["rc"] != 0:
        return ""

    return r["output"].strip()


def detect_shell(container):
    r = host_run(
        [
            "docker",
            "exec",
            container,
            "/bin/sh",
            "-lc",
            "if [ -x /bin/bash ]; then echo /bin/bash; "
            "else echo /bin/sh; fi",
        ],
        timeout=30,
    )

    shell = r["output"].strip()

    if shell not in ("/bin/bash", "/bin/sh"):
        return "/bin/sh"

    return shell


def discover_git_repos(container, shell, workdir):
    candidates = []

    for p in [
        workdir,
        "/testbed",
        "/workspace",
        "/app",
        "/repo",
        "/repository",
    ]:
        if p and p not in candidates:
            candidates.append(p)

    find_cmd = r"""
find / -maxdepth 4 \
  \( -type d -o -type f \) \
  -name .git \
  -print 2>/dev/null | head -100
"""

    r = dexec(
        container,
        shell,
        find_cmd,
        timeout=120,
    )

    for line in r["output"].splitlines():
        line = line.strip()

        if not line:
            continue

        p = str(Path(line).parent)

        if p not in candidates:
            candidates.append(p)

    repos = []

    for p in candidates:
        cmd = (
            f"git -C {shlex.quote(p)} "
            "rev-parse --show-toplevel"
        )

        rr = dexec(
            container,
            shell,
            cmd,
            timeout=30,
        )

        if rr["rc"] == 0:
            top = rr["output"].strip().splitlines()[-1]

            if top not in repos:
                repos.append(top)

    return repos


def has_commit(container, shell, repo, commit):
    cmd = (
        f"git -C {shlex.quote(repo)} "
        f"cat-file -e {shlex.quote(commit + '^{commit}')}"
    )

    return dexec(
        container,
        shell,
        cmd,
        timeout=30,
    )["rc"] == 0


def choose_repo(container, shell, repos, base_commit, repo_name):
    containing = [
        r
        for r in repos
        if has_commit(
            container,
            shell,
            r,
            base_commit,
        )
    ]

    want = repo_name.split("/")[-1].lower().replace("-", "").replace("_", "")

    def score(path):
        base = Path(path).name.lower().replace("-", "").replace("_", "")
        return int(base == want)

    if containing:
        containing.sort(
            key=score,
            reverse=True,
        )

        return containing[0], True, containing

    if repos:
        repos = sorted(
            repos,
            key=score,
            reverse=True,
        )

        return repos[0], False, []

    return None, False, []


def get_head(container, shell, repo):
    r = dexec(
        container,
        shell,
        f"git -C {shlex.quote(repo)} rev-parse HEAD",
        timeout=30,
    )

    return (
        r["output"].strip()
        if r["rc"] == 0
        else None
    )


def git_status(container, shell, repo):
    r = dexec(
        container,
        shell,
        f"git -C {shlex.quote(repo)} status --porcelain",
        timeout=60,
    )

    return r["output"]


def reset_base(container, shell, repo, base_commit):
    cmd = (
        f"git -C {shlex.quote(repo)} "
        f"reset --hard {shlex.quote(base_commit)}"
    )

    return dexec(
        container,
        shell,
        cmd,
        timeout=120,
    )


def git_apply(container, repo, patch, threeway=False):
    patch = patch or ""

    result = {
        "empty": not patch.strip(),
        "bytes": len(patch.encode("utf-8", errors="surrogatepass")),
        "sha256": sha256_text(patch),
        "check_rc": None,
        "apply_rc": None,
        "check_output": "",
        "apply_output": "",
        "ok": False,
        "threeway": threeway,
    }

    if not patch.strip():
        result["check_rc"] = 0
        result["apply_rc"] = 0
        result["ok"] = True
        return result

    raw = patch.encode(
        "utf-8",
        errors="surrogatepass",
    )

    check = host_run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "git",
            "-C",
            repo,
            "apply",
            *(["--3way"] if threeway else []),
            "--check",
            "-",
        ],
        timeout=120,
        input_bytes=raw,
    )

    result["check_rc"] = check["rc"]
    result["check_output"] = check["output"]

    if check["rc"] != 0:
        return result

    apply = host_run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "git",
            "-C",
            repo,
            "apply",
            *(["--3way"] if threeway else []),
            "-",
        ],
        timeout=120,
        input_bytes=raw,
    )

    result["apply_rc"] = apply["rc"]
    result["apply_output"] = apply["output"]
    result["ok"] = apply["rc"] == 0

    return result


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clean_log(s):
    return ANSI.sub("", s or "")


def test_aliases(test_id):
    aliases = [test_id]

    if "::" in test_id:
        parts = test_id.split("::")

        if len(parts) >= 2:
            aliases.append(
                Path(parts[0]).name
                + "::"
                + "::".join(parts[1:])
            )

        if len(parts) >= 3:
            aliases.append("::".join(parts[-2:]))

        aliases.append(parts[-1])

    elif "." in test_id:
        aliases.append(test_id.split(".")[-1])

    out = []

    for x in aliases:
        if x and x not in out:
            out.append(x)

    return out


def find_pytest_runner(test_cmds):
    pattern = re.compile(
        r"(python(?:\d+(?:\.\d+)?)?\s+-m\s+pytest|pytest)\b"
    )

    for cmd in test_cmds:
        m = pattern.search(cmd)

        if m:
            prefix = cmd[:m.start()]
            runner = m.group(1)
            return prefix + runner

    return None


def probe_collection(
    container,
    shell,
    repo,
    runner,
    test_id,
    timeout,
):
    if not runner:
        return {
            "available": False,
            "rc": None,
            "timeout": False,
            "output": "",
            "collected": False,
        }

    command = (
        f"cd {shlex.quote(repo)} && "
        f"{runner} --collect-only -q {shlex.quote(test_id)}"
    )

    r = dexec(
        container,
        shell,
        command,
        timeout=timeout,
    )

    log = clean_log(r["output"])

    aliases = test_aliases(test_id)

    seen = any(
        alias in log
        for alias in aliases
        if len(alias) >= 6
    )

    collected = (
        r["rc"] == 0
        and seen
    )

    return {
        "available": True,
        "rc": r["rc"],
        "timeout": r["timeout"],
        "output": r["output"][-10000:],
        "collected": collected,
    }


def run_tests(
    container,
    shell,
    repo,
    bug,
    log_file,
    timeout,
    collect_timeout,
):
    test_cmds = as_list(
        bug.get("test_cmds")
    )

    f2p = as_list(
        bug.get("FAIL_TO_PASS")
    )

    p2p = as_list(
        bug.get("PASS_TO_PASS")
    )

    declared = f2p + p2p
    command_results = []
    full_log = []

    for i, cmd in enumerate(test_cmds, 1):
        command = (
            f"cd {shlex.quote(repo)} && "
            f"{cmd}"
        )

        r = dexec(
            container,
            shell,
            command,
            timeout=timeout,
        )

        command_results.append(
            {
                "index": i,
                "command": cmd,
                "rc": r["rc"],
                "timeout": r["timeout"],
                "seconds": r["seconds"],
            }
        )

        full_log.append(
            f"\n===== COMMAND {i} =====\n"
            f"{cmd}\n"
            f"RC={r['rc']} TIMEOUT={r['timeout']}\n"
            f"{r['output']}\n"
        )

    text = clean_log(
        "\n".join(full_log)
    )

    log_file.write_text(
        text,
        encoding="utf-8",
        errors="replace",
    )

    # 先从真实 test command 输出判断是否观察到测试。
    suffix_counts = {}

    for tid in declared:
        aliases = test_aliases(tid)

        if aliases:
            suffix = aliases[-1]
            suffix_counts[suffix] = (
                suffix_counts.get(suffix, 0)
                + 1
            )

    observed = {}
    unresolved = []

    for tid in declared:
        method = None

        if tid in text:
            method = "exact_test_log"

        else:
            aliases = test_aliases(tid)

            for alias in aliases[1:]:
                if len(alias) < 6:
                    continue

                if (
                    alias == aliases[-1]
                    and suffix_counts.get(alias, 0) > 1
                ):
                    continue

                if alias in text:
                    method = "unique_alias_test_log"
                    break

        if method:
            observed[tid] = {
                "collected": True,
                "method": method,
            }
        else:
            unresolved.append(tid)

    # 对 test log 中仍不可观测的 pytest-style test
    # 额外执行 collect-only probe。
    # 该 probe 只用于验证 test 是否存在，不参与 Gold 评分。
    runner = find_pytest_runner(test_cmds)

    for tid in unresolved:
        probe = probe_collection(
            container,
            shell,
            repo,
            runner,
            tid,
            collect_timeout,
        )

        if probe["collected"]:
            observed[tid] = {
                "collected": True,
                "method": "pytest_collect_probe",
                "probe_rc": probe["rc"],
            }
        else:
            observed[tid] = {
                "collected": False,
                "method": (
                    "pytest_collect_failed"
                    if probe["available"]
                    else "no_supported_collector"
                ),
                "probe_rc": probe["rc"],
                "probe_timeout": probe["timeout"],
                "probe_output": probe["output"],
            }

    missing = [
        tid
        for tid in declared
        if not observed.get(tid, {}).get(
            "collected",
            False,
        )
    ]

    command_ok = (
        bool(test_cmds)
        and all(
            x["rc"] == 0
            and not x["timeout"]
            for x in command_results
        )
    )

    all_collected = (
        len(missing) == 0
    )

    return {
        "test_cmds": test_cmds,
        "test_command_noop": all(
            is_noop_test_command(cmd)
            for cmd in test_cmds
        ),
        "command_results": command_results,
        "commands_all_zero": command_ok,
        "f2p": f2p,
        "p2p": p2p,
        "declared_count": len(declared),
        "observed": observed,
        "missing_tests": missing,
        "all_tests_collected": all_collected,

        # Gold 后 test command 全部为 0，
        # 且对应声明测试均已确认可观测，
        # 才允许记作 F2P/P2P pass。
        "f2p_all_pass": (
            command_ok
            and all(
                observed.get(t, {}).get(
                    "collected",
                    False,
                )
                for t in f2p
            )
        ),
        "p2p_all_pass": (
            command_ok
            and all(
                observed.get(t, {}).get(
                    "collected",
                    False,
                )
                for t in p2p
            )
        ),
    }


def is_noop_test_command(command):
    value = (command or "").strip()

    while value.startswith("cd ") and "&&" in value:
        value = value.split("&&", 1)[1].strip()

    return value in {
        "true",
        ":",
        "/bin/true",
    }


def blocked_stage(bug, reason):
    return {
        "order": bug.get("order"),
        "instance_id": bug.get(
            "swebench_instance_id"
        ),
        "blocked": True,
        "blocked_reason": reason,
        "gold_valid": False,
    }


def replay_chain(
    row,
    out_dir,
    test_timeout,
    collect_timeout,
    start_state="declared-base",
    apply_threeway=False,
    keep_containers=False,
):
    chain_id = row["continuous_id"]
    image = row["docker_image"]
    base_commit = row["base_commit"]
    repo_name = row["repo"]

    safe_chain = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        chain_id,
    )

    chain_log_dir = (
        out_dir
        / "logs"
        / safe_chain
    )

    chain_log_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    container = (
        "chainswe-gold-"
        + safe_chain[:45]
        + "-"
        + uuid.uuid4().hex[:8]
    )

    result = {
        "continuous_id": chain_id,
        "repo": repo_name,
        "docker_image": image,
        "base_commit": base_commit,
        "container": container,
        "started_at": time.time(),
        "stages": [],
        "chain_gold_valid": False,
    }

    start = host_run(
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
            "trap : TERM INT; "
            "while :; do sleep 3600; done",
        ],
        timeout=120,
    )

    result["container_start"] = {
        "rc": start["rc"],
        "output": start["output"],
    }

    if start["rc"] != 0:
        result["hard_failure"] = "CONTAINER_START_FAILED"

        for bug in row["bug_fixes"]:
            result["stages"].append(
                blocked_stage(
                    bug,
                    "CONTAINER_START_FAILED",
                )
            )

        return result

    try:
        shell = detect_shell(container)
        result["shell"] = shell

        workdir = image_workdir(image)
        result["image_workdir"] = workdir

        repos = discover_git_repos(
            container,
            shell,
            workdir,
        )

        result["git_repo_candidates"] = repos

        repo, base_exists, containing = choose_repo(
            container,
            shell,
            repos,
            base_commit,
            repo_name,
        )

        result["repo_path"] = repo
        result["base_commit_exists"] = base_exists
        result["repos_containing_base"] = containing

        if not repo:
            result["hard_failure"] = "GIT_REPO_NOT_FOUND"

            for bug in row["bug_fixes"]:
                result["stages"].append(
                    blocked_stage(
                        bug,
                        "GIT_REPO_NOT_FOUND",
                    )
                )

            return result

        result["baked_head"] = get_head(
            container,
            shell,
            repo,
        )

        result["start_state"] = start_state

        result["initial_git_status"] = git_status(
            container,
            shell,
            repo,
        )

        if (
            not base_exists
            and start_state == "declared-base"
        ):
            result["hard_failure"] = "BASE_COMMIT_NOT_IN_IMAGE"

            for bug in row["bug_fixes"]:
                result["stages"].append(
                    blocked_stage(
                        bug,
                        "BASE_COMMIT_NOT_IN_IMAGE",
                    )
                )

            return result

        if start_state == "declared-base":
            reset = reset_base(
                container,
                shell,
                repo,
                base_commit,
            )

            result["base_reset"] = {
                "rc": reset["rc"],
                "output": reset["output"],
            }

            result["head_after_reset"] = get_head(
                container,
                shell,
                repo,
            )

            if (
                reset["rc"] != 0
                or result["head_after_reset"]
                != base_commit
            ):
                result["hard_failure"] = "BASE_RESET_FAILED"

                for bug in row["bug_fixes"]:
                    result["stages"].append(
                        blocked_stage(
                            bug,
                            "BASE_RESET_FAILED",
                        )
                    )

                return result

        else:
            clean = dexec(
                container,
                shell,
                f"git -C {shlex.quote(repo)} clean -fd",
                timeout=120,
            )

            result["base_reset"] = {
                "rc": None,
                "output": (
                    "skipped: using baked image HEAD"
                ),
            }

            result["baked_clean"] = {
                "rc": clean["rc"],
                "output": clean["output"],
            }

        result["head_after_start"] = get_head(
            container,
            shell,
            repo,
        )

        hard_block = None

        for bug in sorted(
            row["bug_fixes"],
            key=lambda x: x.get("order", 0),
        ):
            if hard_block:
                result["stages"].append(
                    blocked_stage(
                        bug,
                        hard_block,
                    )
                )
                continue

            order = bug.get("order")
            instance_id = bug.get(
                "swebench_instance_id"
            )

            stage = {
                "order": order,
                "instance_id": instance_id,
                "blocked": False,
                "gold_valid": False,
            }

            test_apply = git_apply(
                container,
                repo,
                bug.get("test_patch", ""),
                threeway=apply_threeway,
            )

            stage["test_patch"] = test_apply

            if not test_apply["ok"]:
                stage["failure"] = "TEST_PATCH_APPLY_FAILED"
                result["stages"].append(stage)
                hard_block = "BLOCKED_BY_PRIOR_TEST_PATCH_FAILURE"
                continue

            gold_apply = git_apply(
                container,
                repo,
                bug.get("patch", ""),
                threeway=apply_threeway,
            )

            stage["gold_patch"] = gold_apply

            if not gold_apply["ok"]:
                stage["failure"] = "GOLD_PATCH_APPLY_FAILED"
                result["stages"].append(stage)
                hard_block = "BLOCKED_BY_PRIOR_GOLD_PATCH_FAILURE"
                continue

            test_log = (
                chain_log_dir
                / f"stage_{order:02d}_tests.log"
            )

            tests = run_tests(
                container,
                shell,
                repo,
                bug,
                test_log,
                timeout=test_timeout,
                collect_timeout=collect_timeout,
            )

            stage["tests"] = tests
            stage["git_status_after_stage"] = git_status(
                container,
                shell,
                repo,
            )

            stage["gold_valid"] = (
                test_apply["ok"]
                and gold_apply["ok"]
                and tests["all_tests_collected"]
                and tests["f2p_all_pass"]
                and tests["p2p_all_pass"]
            )

            if not stage["gold_valid"]:
                if not tests["all_tests_collected"]:
                    stage["failure"] = "DECLARED_TEST_NOT_OBSERVABLE"
                elif not tests["commands_all_zero"]:
                    stage["failure"] = "GOLD_TEST_COMMAND_FAILED"
                else:
                    stage["failure"] = "GOLD_VALIDATION_FAILED"

            result["stages"].append(stage)

        result["chain_gold_valid"] = (
            len(result["stages"])
            == len(row["bug_fixes"])
            and all(
                s.get("gold_valid", False)
                for s in result["stages"]
            )
        )

        return result

    finally:
        if not keep_containers:
            host_run(
                [
                    "docker",
                    "rm",
                    "-f",
                    container,
                ],
                timeout=120,
            )

        result["finished_at"] = time.time()
        result["seconds"] = round(
            result["finished_at"]
            - result["started_at"],
            3,
        )


def load_rows(data_path=DATA):
    rows = []

    with Path(data_path).open() as f:
        for line in f:
            rows.append(json.loads(line))

    return rows


def load_chain_ids(path):
    ids = []

    for raw in Path(path).read_text(
        encoding="utf-8"
    ).splitlines():
        line = raw.strip()

        if not line:
            continue

        if line.startswith("| `"):
            parts = line.split("`")

            if len(parts) >= 3:
                ids.append(parts[1])

            continue

        if line.startswith("#") or line.startswith("|"):
            continue

        ids.append(line)

    return list(dict.fromkeys(ids))


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--data",
        default=str(DATA),
        help="ChainSWE-style data.jsonl to replay.",
    )

    ap.add_argument(
        "--out",
        required=True,
    )

    ap.add_argument(
        "--workers",
        type=int,
        default=10,
    )

    ap.add_argument(
        "--test-timeout",
        type=int,
        default=1800,
    )

    ap.add_argument(
        "--collect-timeout",
        type=int,
        default=180,
    )

    ap.add_argument(
        "--start-state",
        choices=[
            "declared-base",
            "baked-head",
        ],
        default="declared-base",
        help=(
            "declared-base 保持 strict 论文回放语义；"
            "baked-head 用 Docker 镜像自带 HEAD 作为 runtime fallback，"
            "只建议用于 forensic audit。"
        ),
    )

    ap.add_argument(
        "--apply-threeway",
        action="store_true",
        help=(
            "使用 git apply --3way 应用 test/gold patch；"
            "只建议用于 transition patch forensic。"
        ),
    )

    ap.add_argument(
        "--chain",
        default=None,
        help=(
            "continuous_id 或 repo；"
            "不指定则运行全部 100 chains"
        ),
    )

    ap.add_argument(
        "--chain-file",
        default=None,
        help=(
            "包含 continuous_id 的文本或 markdown 表；"
            "用于只运行一组 chain。"
        ),
    )

    ap.add_argument(
        "--resume",
        action="store_true",
    )

    ap.add_argument(
        "--keep-containers",
        action="store_true",
        help=(
            "保留每条 chain 的 chainswe-gold-* 容器；"
            "默认完成后删除临时容器（不会删除镜像）。"
        ),
    )

    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    (out_dir / "logs").mkdir(
        exist_ok=True,
    )

    results_file = (
        out_dir / "chains.jsonl"
    )

    rows = load_rows(args.data)

    if args.chain:
        rows = [
            r
            for r in rows
            if (
                r["continuous_id"] == args.chain
                or r["repo"] == args.chain
                or args.chain in r["continuous_id"]
            )
        ]

    if args.chain_file:
        requested = set(load_chain_ids(args.chain_file))
        rows = [
            r
            for r in rows
            if r["continuous_id"] in requested
        ]

        missing = sorted(
            requested
            - {
                r["continuous_id"]
                for r in rows
            }
        )

        if missing:
            raise SystemExit(
                "Unknown chain ids in --chain-file: "
                + ", ".join(missing)
            )

    if not rows:
        raise SystemExit(
            "No matching chains."
        )

    finished = set()

    if results_file.exists():
        if not args.resume:
            raise SystemExit(
                f"{results_file} already exists. "
                "Use --resume or choose a new --out."
            )

        with results_file.open() as f:
            for line in f:
                try:
                    x = json.loads(line)
                    finished.add(
                        x["continuous_id"]
                    )
                except Exception:
                    pass

    rows = [
        r
        for r in rows
        if r["continuous_id"]
        not in finished
    ]

    print(
        f"remaining chains: {len(rows)}",
        flush=True,
    )

    if not rows:
        print(
            "Nothing to do.",
            flush=True,
        )
        return

    with results_file.open(
        "a",
        buffering=1,
    ) as fp:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers
        ) as ex:
            futures = {
                ex.submit(
                    replay_chain,
                    row,
                    out_dir,
                    args.test_timeout,
                    args.collect_timeout,
                    args.start_state,
                    args.apply_threeway,
                    args.keep_containers,
                ): row
                for row in rows
            }

            done = 0

            for fut in concurrent.futures.as_completed(
                futures
            ):
                row = futures[fut]

                try:
                    result = fut.result()
                except Exception as e:
                    result = {
                        "continuous_id": row[
                            "continuous_id"
                        ],
                        "repo": row["repo"],
                        "docker_image": row[
                            "docker_image"
                        ],
                        "base_commit": row[
                            "base_commit"
                        ],
                        "chain_gold_valid": False,
                        "runner_exception": repr(e),
                        "stages": [],
                    }

                fp.write(
                    json.dumps(
                        result,
                        ensure_ascii=False,
                    )
                    + "\n"
                )

                fp.flush()

                done += 1

                valid = result.get(
                    "chain_gold_valid",
                    False,
                )

                print(
                    f"[{done}/{len(rows)}] "
                    f"{row['continuous_id']} "
                    f"gold_valid={valid}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
