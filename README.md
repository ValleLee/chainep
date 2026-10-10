# ChainSWE experiments

This repository contains the ChainSWE benchmark, experiment code, and the local SWE-Edit agent implementation used for evaluation.

- `ChainSWE/data.jsonl`: the core dataset (100 chains, 304 bug-fix tasks). See [ChainSWE/README.md](ChainSWE/README.md).
- `ChainSWE/chain_list_30.txt`, `docker_images_30.txt`, and experiment manifests: selected chain and image references.
- `ChainSWE/scripts/`, `configs/`, `analysis/`, and `experiments/`: runners, configuration templates, reports, and compact result summaries. Raw trajectories, gateway requests and responses, logs, and temporary output are excluded.
- `SWE-Edit/src/`, `tests/`, `pyproject.toml`, and `uv.lock`: agent code and dependencies. See [SWE-Edit/README.md](SWE-Edit/README.md).

Create your own local API configuration. Credential files such as `*.env` are excluded from Git.
