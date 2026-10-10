#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from chainswe_exp.reporting import summarize


def main():
    parser = argparse.ArgumentParser(description="Summarize one ChainSWE experiment run")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.run_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
