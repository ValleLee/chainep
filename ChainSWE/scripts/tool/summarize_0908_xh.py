#!/usr/bin/env python3
"""Summarize the 0908 XH run using the tested 0907 aggregation logic."""

from __future__ import annotations

import os
import argparse
import runpy
import sys
from pathlib import Path


REFERENCE = Path(__file__).resolve().parents[2] / "experiments" / "0907test_xh_on30" / "summarize_0907_metrics.py"


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    args, rest = parser.parse_known_args()

    sys.argv = [str(REFERENCE), "--run-dir", str(args.run_dir)]
    if args.out_dir is not None:
        sys.argv += ["--out-dir", str(args.out_dir)]
    sys.argv += rest
    try:
        runpy.run_path(str(REFERENCE), run_name="__main__")
    except SystemExit as exc:
        rc = int(exc.code or 0)
    else:
        rc = 0

    out_dir = args.out_dir or args.run_dir / "aggregate"
    old_summary = out_dir / "summary_0907_metrics.json"
    new_summary = out_dir / "summary_0908_xh_metrics.json"
    if old_summary.is_file():
        old_summary.replace(new_summary)
    report = out_dir / "REPORT.md"
    if report.is_file():
        report.write_text(
            report.read_text(encoding="utf-8").replace("# 0907 XH 30-chain metrics", "# 0908 XH 30-chain metrics"),
            encoding="utf-8",
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
