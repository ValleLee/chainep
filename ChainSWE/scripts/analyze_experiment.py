#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from chainswe_exp.reporting import analyze


def main():
    parser = argparse.ArgumentParser(description="Cluster-bootstrap ChainSWE experiment metrics")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=10000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260828)
    args = parser.parse_args()
    result = analyze(args.run_dir, args.bootstrap_samples, args.bootstrap_seed)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
