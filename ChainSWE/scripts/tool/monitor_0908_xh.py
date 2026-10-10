#!/usr/bin/env python3
"""Monitor the 0908 XH run using the tested ChainSWE monitor."""

from __future__ import annotations

import os
import sys
from pathlib import Path


REFERENCE = Path(__file__).resolve().parents[2] / "experiments" / "0907test_xh_on30" / "monitor_0907.py"


if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, str(REFERENCE), *sys.argv[1:]])
