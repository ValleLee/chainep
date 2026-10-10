#!/usr/bin/env python3

import json
from pathlib import Path

ROOT = Path("/lg-home/cog8/lwli6/chain")
DATA = ROOT / "ChainSWE" / "data.jsonl"
OUT = ROOT / "docker_images.txt"

images = set()

with DATA.open() as f:
    for line in f:
        row = json.loads(line)
        images.add(row["docker_image"])

images = sorted(images)

with OUT.open("w") as f:
    for image in images:
        f.write(image + "\n")

print(f"Source: {DATA}")
print(f"Unique Docker images: {len(images)}")
print(f"Saved to: {OUT}")
