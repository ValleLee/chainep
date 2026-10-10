from __future__ import annotations

import copy
import os
import shlex
import shutil
import time
import traceback
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .common import (
    compute_cost,
    flatten_usage,
    json_dump_atomic,
    render_prompt,
    safe_name,
    sha256_text,
    sorted_bugs,
    token_delta,
)
from .docker_runtime import AgentProcess, ContainerRuntime

VALID_MODES = {"oracle", "seq", "seq_mem"}


def materialize_model_env(model: dict[str, Any]) -> dict[str, str]:
    api_type = str(model.get("api_type", "")).upper()
    if api_type not in {"OPENAI", "AZURE_OPENAI", "ANTHROPIC"}:
        raise ValueError(f"Unsupported api_type for {model.get('name')}: {api_type}")
    model_id = model.get("model_id")
    if not model_id:
        raise ValueError(f"model_id missing for {model.get('name')}")
    result = {"API_TYPE": api_type, "MODEL": str(model_id)}

    env_map = model.get("env", {})
    if not isinstance(env_map, dict):
        raise ValueError(f"env must be an object for {model.get('name')}")
    for target, source in env_map.items():
        if not isinstance(target, str) or not isinstance(source, str):
            raise ValueError("model env entries must map target name to source environment name")
        value = os.environ.get(source)
        if value is None:
            raise ValueError(f"Required environment variable is missing: {source}")
        result[target] = value

    values = model.get("values", {})
    if not isinstance(values, dict):
        raise ValueError(f"values must be an object for {model.get('name')}")
    for key, value in values.items():
        if value is not None:
            result[str(key)] = str(value)
    return result


def public_model_snapshot(model: dict[str, Any]) -> dict[str, Any]:
    snapshot = copy.deepcopy(model)
    # The config stores only environment-variable names, never secret values.
    return snapshot


def copy_trajectory_snapshot(runtime: ContainerRuntime, session_id: str, stage_dir: Path) -> None:
    source = runtime.output_dir / ".agent_state" / session_id
    target = stage_dir / "trajectory"
    target.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for item in source.iterdir():
        if item.is_file():
            shutil.copy2(item, target / item.name)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", errors="surrogatepass")


def prepare_oracle_history(runtime: ContainerRuntime, bugs: list[dict[str, Any]], position: int) -> None:
    for previous in bugs[: position - 1]:
        test_apply = runtime.apply_patch(str(previous.get("test_patch", "")), threeway=True)
        if not test_apply["ok"]:
            raise RuntimeError(
                f"ORACLE prior test patch failed for {previous['swebench_instance_id']}: {test_apply['output']}"
            )
        runtime.commit_all(f"oracle test {previous['swebench_instance_id']}")
        gold_apply = runtime.apply_patch(str(previous.get("patch", "")), threeway=True)
        if not gold_apply["ok"]:
            raise RuntimeError(
                f"ORACLE prior gold patch failed for {previous['swebench_instance_id']}: {gold_apply['output']}"
            )
        runtime.commit_all(f"oracle gold {previous['swebench_instance_id']}")


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            import json

            parsed = json.loads(stripped)
        except Exception:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [value]
    return [str(value)]


def run_official_tests(
    runtime: ContainerRuntime,
    bug: dict[str, Any],
    log_file: Path,
    timeout: int,
) -> dict[str, Any]:
    test_cmds = as_list(bug.get("test_cmds"))
    f2p = as_list(bug.get("FAIL_TO_PASS"))
    p2p = as_list(bug.get("PASS_TO_PASS"))
    command_results: list[dict[str, Any]] = []
    logs: list[str] = []
    for index, cmd in enumerate(test_cmds, 1):
        result = runtime.exec(cmd, timeout=timeout)
        command_results.append(
            {
                "index": index,
                "command": cmd,
                "rc": result["rc"],
                "timeout": result["timeout"],
                "seconds": result["seconds"],
            }
        )
        logs.append(
            f"\n===== COMMAND {index} =====\n"
            f"{cmd}\nRC={result['rc']} TIMEOUT={result['timeout']}\n"
            f"{result['output']}\n"
        )
    write_text(log_file, "\n".join(logs))
    commands_all_zero = bool(test_cmds) and all(
        item["rc"] == 0 and not item["timeout"]
        for item in command_results
    )
    return {
        "test_cmds": test_cmds,
        "command_results": command_results,
        "commands_all_zero": commands_all_zero,
        "f2p": f2p,
        "p2p": p2p,
        "f2p_total": len(f2p),
        "p2p_total": len(p2p),
        "f2p_all_pass": commands_all_zero,
        "p2p_all_pass": commands_all_zero,
        "all_tests_collected": None,
        "grading_mode": "chainswe_command",
        "audit_note": "F2P/P2P are treated as semantic diagnostics; official test_cmds exit status is authoritative.",
    }


def classify_result(
    agent_status: str,
    test_patch: dict[str, Any],
    tests: dict[str, Any] | None,
) -> tuple[bool, str]:
    if not test_patch.get("ok"):
        return False, "TEST_PATCH_APPLY_FAILED"
    if tests is None:
        return False, "TEST_NOT_RUN"
    if any(item.get("timeout") for item in tests.get("command_results", [])):
        return False, "TEST_TIMEOUT"
    if tests.get("commands_all_zero"):
        return True, "RESOLVED"
    if agent_status == "timeout":
        return False, "AGENT_TIMEOUT"
    if agent_status not in {"completed"}:
        return False, "AGENT_ERROR"
    return False, "TEST_COMMAND_FAILED"


def run_stage(
    *,
    runtime: ContainerRuntime,
    bug: dict[str, Any],
    mode: str,
    model: dict[str, Any],
    agent_type: str,
    reasoning_effort: str,
    agent_timeout: int,
    test_timeout: int,
    collect_timeout: int,
    stage_dir: Path,
    agent_process: AgentProcess | None,
    session_id: str,
    prior_unresolved: bool,
) -> tuple[dict[str, Any], AgentProcess | None]:
    started = time.time()
    stage_dir.mkdir(parents=True, exist_ok=True)
    instance_id = str(bug["swebench_instance_id"])
    position = int(bug["order"])
    problem = str(bug["problem_statement"])
    prompt = render_prompt(problem, str(runtime.repo_path))
    write_text(stage_dir / "problem_statement.txt", problem)
    write_text(stage_dir / "prompt.txt", prompt)

    before_head = runtime.head()
    if not before_head:
        raise RuntimeError("Missing HEAD before agent")
    before_status = runtime.status()
    base_commit = str(runtime.image_info["base_commit"])
    write_text(stage_dir / "repo_before.patch", runtime.diff(base_commit, before_head))
    json_dump_atomic(
        stage_dir / "git_state_before.json",
        {"head": before_head, "status": before_status, "base_commit": base_commit},
    )

    if agent_process is None:
        agent_process = AgentProcess(
            runtime,
            materialize_model_env(model),
            stage_dir / "agent_bridge.log",
        )
    response = agent_process.request(
        {
            "command": "run",
            "agent_type": agent_type,
            "session_id": session_id,
            "reasoning_effort": reasoning_effort,
            "save_dir": "/chainswe-output/.agent_state",
            "workdir": runtime.repo_path,
            "prompt": prompt,
            "timeout": agent_timeout,
            "reset_context": False,
        },
        timeout=agent_timeout + 120,
    )
    copy_trajectory_snapshot(runtime, session_id, stage_dir)
    if response.get("status") in {"bridge_error", "error"}:
        raise RuntimeError(f"Agent infrastructure error: {response.get('error')}")

    token_stats = token_delta(
        response.get("token_stats_after", {}),
        response.get("token_stats_before", {}),
    )
    usage = flatten_usage(token_stats)
    usage["cost_usd"] = compute_cost(usage, model)
    json_dump_atomic(stage_dir / "usage.json", {"raw": token_stats, "normalized": usage})

    model_head = runtime.commit_all(f"agent {instance_id}", clean_agent_artifacts=True)
    model_patch = runtime.diff(before_head, model_head)
    write_text(stage_dir / "model.patch", model_patch)

    test_apply = runtime.apply_patch(str(bug.get("test_patch", "")), threeway=True)
    tests = None
    if test_apply["ok"]:
        runtime.commit_all(f"test patch {instance_id}")
        tests = run_official_tests(
            runtime,
            bug,
            stage_dir / "test.log",
            timeout=test_timeout,
        )
        runtime.cleanup_test_artifacts()
    else:
        write_text(stage_dir / "test.log", "TEST_PATCH_APPLY_FAILED\n" + test_apply.get("output", ""))

    resolved, failure_type = classify_result(str(response.get("status")), test_apply, tests)
    after_head = runtime.head()
    after_status = runtime.status()
    if after_head:
        write_text(stage_dir / "repo_after.patch", runtime.diff(base_commit, after_head))
    json_dump_atomic(
        stage_dir / "git_state_after.json",
        {"head": after_head, "status": after_status, "base_commit": base_commit},
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "position": position,
        "instance_id": instance_id,
        "mode": mode,
        "model_name": model["name"],
        "model_id": model["model_id"],
        "agent_type": agent_type,
        "reasoning_effort": reasoning_effort,
        "agent_status": response.get("status"),
        "agent_error": response.get("error"),
        "resolved": resolved,
        "failure_type": failure_type,
        "prior_unresolved": prior_unresolved,
        "recovered": bool(prior_unresolved and resolved),
        "repo_head_before": before_head,
        "repo_head_model": model_head,
        "repo_head_after": after_head,
        "git_status_before": before_status,
        "git_status_after": after_status,
        "model_patch_sha256": sha256_text(model_patch),
        "model_patch_bytes": len(model_patch.encode("utf-8", errors="surrogatepass")),
        "test_patch": test_apply,
        "tests": tests,
        "usage": usage,
        "tool_call_stats": response.get("tool_call_stats", {}),
        "started_at": started,
        "finished_at": time.time(),
    }
    result["seconds"] = round(result["finished_at"] - started, 3)
    json_dump_atomic(stage_dir / "result.json", result)
    return result, agent_process


def run_chain_mode(
    *,
    row: dict[str, Any],
    mode: str,
    model: dict[str, Any],
    output_dir: Path,
    wheel: Path,
    agent_server: Path,
    agent_type: str,
    reasoning_effort: str,
    agent_timeout: int,
    test_timeout: int,
    collect_timeout: int,
) -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode: {mode}")
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    bugs = sorted_bugs(row)
    chain_result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "complete": False,
        "continuous_id": row["continuous_id"],
        "repo": row["repo"],
        "docker_image": row["docker_image"],
        "base_commit": row["base_commit"],
        "mode": mode,
        "model_name": model["name"],
        "model_id": model["model_id"],
        "started_at": started,
        "stages": [],
    }

    persistent_runtime: ContainerRuntime | None = None
    persistent_agent: AgentProcess | None = None
    prior_unresolved = False
    try:
        if mode in {"seq", "seq_mem"}:
            persistent_runtime = ContainerRuntime(
                image=row["docker_image"],
                chain_id=row["continuous_id"],
                mode=mode,
                output_dir=output_dir,
                wheel=wheel,
                agent_server=agent_server,
            )
            persistent_runtime.start(row["repo"], row["base_commit"])

        for bug in bugs:
            position = int(bug["order"])
            instance_id = str(bug["swebench_instance_id"])
            stage_dir = output_dir / "stages" / f"{position:02d}_{safe_name(instance_id)}"
            runtime = persistent_runtime
            oracle_runtime: ContainerRuntime | None = None
            stage_agent = persistent_agent if mode == "seq_mem" else None
            session_id = (
                f"{safe_name(row['continuous_id'])}_seq_mem"
                if mode == "seq_mem"
                else f"{safe_name(instance_id)}_{mode}"
            )
            try:
                if mode == "oracle":
                    oracle_runtime = ContainerRuntime(
                        image=row["docker_image"],
                        chain_id=row["continuous_id"],
                        mode=f"oracle{position}",
                        output_dir=output_dir,
                        wheel=wheel,
                        agent_server=agent_server,
                    )
                    oracle_runtime.start(row["repo"], row["base_commit"])
                    prepare_oracle_history(oracle_runtime, bugs, position)
                    runtime = oracle_runtime
                assert runtime is not None
                stage_result, stage_agent = run_stage(
                    runtime=runtime,
                    bug=bug,
                    mode=mode,
                    model=model,
                    agent_type=agent_type,
                    reasoning_effort=reasoning_effort,
                    agent_timeout=agent_timeout,
                    test_timeout=test_timeout,
                    collect_timeout=collect_timeout,
                    stage_dir=stage_dir,
                    agent_process=stage_agent,
                    session_id=session_id,
                    prior_unresolved=prior_unresolved if mode != "oracle" else False,
                )
            except BaseException as exc:
                stage_result = {
                    "schema_version": SCHEMA_VERSION,
                    "position": position,
                    "instance_id": instance_id,
                    "mode": mode,
                    "model_name": model["name"],
                    "model_id": model["model_id"],
                    "resolved": False,
                    "failure_type": "INFRA_ERROR",
                    "infra_error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                    "prior_unresolved": prior_unresolved if mode != "oracle" else False,
                    "recovered": False,
                }
                json_dump_atomic(stage_dir / "result.json", stage_result)
                # Infra failure invalidates this chain-mode. Continue creating explicit records,
                # but never mix them into model metrics.
            finally:
                if mode == "oracle" and stage_agent is not None:
                    stage_agent.close()
                if mode == "oracle" and oracle_runtime is not None:
                    oracle_runtime.close()
            chain_result["stages"].append(stage_result)
            if stage_result.get("failure_type") != "INFRA_ERROR":
                prior_unresolved = prior_unresolved or not bool(stage_result.get("resolved"))

            if mode == "seq_mem":
                persistent_agent = stage_agent
            elif stage_agent is not None:
                stage_agent.close()

        chain_result["complete"] = len(chain_result["stages"]) == len(bugs) and all(
            stage.get("failure_type") != "INFRA_ERROR" for stage in chain_result["stages"]
        )
        chain_result["actual_chain_resolved"] = bool(
            chain_result["complete"] and all(stage.get("resolved") for stage in chain_result["stages"])
        )
        return chain_result
    finally:
        if persistent_agent is not None:
            persistent_agent.close()
        if persistent_runtime is not None:
            persistent_runtime.close()
        chain_result["finished_at"] = time.time()
        chain_result["seconds"] = round(chain_result["finished_at"] - started, 3)
        json_dump_atomic(output_dir / "chain_result.json", chain_result)
