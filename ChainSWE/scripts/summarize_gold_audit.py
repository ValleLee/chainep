#!/usr/bin/env python3

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


ap = argparse.ArgumentParser()

ap.add_argument(
    "--run-dir",
    required=True,
)

args = ap.parse_args()

run_dir = Path(args.run_dir)
src = run_dir / "chains.jsonl"

chains = []

with src.open() as f:
    for line in f:
        chains.append(json.loads(line))

stages = []
missing_rows = []
failure_counter = Counter()
noop_test_counter = Counter()


def is_noop_test_command(command):
    value = (command or "").strip()

    while value.startswith("cd ") and "&&" in value:
        value = value.split("&&", 1)[1].strip()

    return value in {
        "true",
        ":",
        "/bin/true",
    }

for chain in chains:
    chain_id = chain["continuous_id"]

    if chain.get("hard_failure"):
        failure_counter[
            chain["hard_failure"]
        ] += 1

    if chain.get("runner_exception"):
        failure_counter[
            "RUNNER_EXCEPTION"
        ] += 1

    for stage in chain.get("stages", []):
        tests = stage.get("tests", {})
        failure = stage.get("failure")
        test_cmds = tests.get(
            "test_cmds",
            [],
        )
        test_command_noop = (
            tests.get("test_command_noop")
            if "test_command_noop" in tests
            else (
                bool(test_cmds)
                and all(
                    is_noop_test_command(cmd)
                    for cmd in test_cmds
                )
            )
        )

        if test_command_noop:
            noop_test_counter["stages"] += 1

        if failure:
            failure_counter[failure] += 1

        row = {
            "continuous_id": chain_id,
            "repo": chain.get("repo"),
            "docker_image": chain.get(
                "docker_image"
            ),
            "base_commit": chain.get(
                "base_commit"
            ),
            "baked_head": chain.get(
                "baked_head"
            ),
            "base_commit_exists": chain.get(
                "base_commit_exists"
            ),
            "order": stage.get("order"),
            "instance_id": stage.get(
                "instance_id"
            ),
            "blocked": stage.get(
                "blocked",
                False,
            ),
            "blocked_reason": stage.get(
                "blocked_reason",
                "",
            ),
            "test_patch_ok": stage.get(
                "test_patch",
                {},
            ).get("ok"),
            "gold_patch_ok": stage.get(
                "gold_patch",
                {},
            ).get("ok"),
            "test_commands_all_zero": tests.get(
                "commands_all_zero"
            ),
            "test_command_noop": test_command_noop,
            "f2p_count": len(
                tests.get("f2p", [])
            ),
            "p2p_count": len(
                tests.get("p2p", [])
            ),
            "missing_test_count": len(
                tests.get(
                    "missing_tests",
                    [],
                )
            ),
            "all_tests_collected": tests.get(
                "all_tests_collected"
            ),
            "f2p_all_pass": tests.get(
                "f2p_all_pass"
            ),
            "p2p_all_pass": tests.get(
                "p2p_all_pass"
            ),
            "gold_valid": stage.get(
                "gold_valid",
                False,
            ),
            "failure": failure or "",
        }

        stages.append(row)

        observed = tests.get(
            "observed",
            {},
        )

        f2p = set(
            tests.get("f2p", [])
        )

        p2p = set(
            tests.get("p2p", [])
        )

        for tid in tests.get(
            "missing_tests",
            [],
        ):
            info = observed.get(
                tid,
                {},
            )

            missing_rows.append(
                {
                    "continuous_id": chain_id,
                    "repo": chain.get(
                        "repo"
                    ),
                    "order": stage.get(
                        "order"
                    ),
                    "instance_id": stage.get(
                        "instance_id"
                    ),
                    "test_id": tid,
                    "test_type": (
                        "F2P"
                        if tid in f2p
                        else (
                            "P2P"
                            if tid in p2p
                            else "UNKNOWN"
                        )
                    ),
                    "method": info.get(
                        "method"
                    ),
                    "probe_rc": info.get(
                        "probe_rc"
                    ),
                    "probe_timeout": info.get(
                        "probe_timeout"
                    ),
                }
            )


stage_csv = run_dir / "stages.csv"

fields = [
    "continuous_id",
    "repo",
    "docker_image",
    "base_commit",
    "baked_head",
    "base_commit_exists",
    "order",
    "instance_id",
    "blocked",
    "blocked_reason",
    "test_patch_ok",
    "gold_patch_ok",
    "test_commands_all_zero",
    "test_command_noop",
    "f2p_count",
    "p2p_count",
    "missing_test_count",
    "all_tests_collected",
    "f2p_all_pass",
    "p2p_all_pass",
    "gold_valid",
    "failure",
]

with stage_csv.open(
    "w",
    newline="",
) as f:
    w = csv.DictWriter(
        f,
        fieldnames=fields,
    )

    w.writeheader()
    w.writerows(stages)


missing_file = (
    run_dir
    / "missing_tests.jsonl"
)

with missing_file.open("w") as f:
    for x in missing_rows:
        f.write(
            json.dumps(
                x,
                ensure_ascii=False,
            )
            + "\n"
        )


valid_chains = sum(
    bool(c.get("chain_gold_valid"))
    for c in chains
)

valid_stages = sum(
    bool(s.get("gold_valid"))
    for s in stages
)

summary = {
    "chains_observed": len(chains),
    "chains_gold_valid": valid_chains,
    "chains_gold_invalid": (
        len(chains)
        - valid_chains
    ),
    "stages_observed": len(stages),
    "stages_gold_valid": valid_stages,
    "stages_gold_invalid": (
        len(stages)
        - valid_stages
    ),
    "missing_test_ids": len(
        missing_rows
    ),
    "noop_test_command_stages": noop_test_counter[
        "stages"
    ],
    "failure_counts": dict(
        failure_counter
    ),
}

with (
    run_dir
    / "summary.json"
).open("w") as f:
    json.dump(
        summary,
        f,
        indent=2,
        ensure_ascii=False,
    )


print(
    json.dumps(
        summary,
        indent=2,
        ensure_ascii=False,
    )
)
