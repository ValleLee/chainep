#!/usr/bin/env python3
import csv
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path("/lg-home/cog8/lwli6/chain")
IMAGES = [
    x.strip()
    for x in (ROOT / "docker_images.txt").read_text().splitlines()
    if x.strip()
]

OUT = ROOT / "ChainSWE/logs/docker_debug/image_state_changes.csv"
OUT.parent.mkdir(parents=True, exist_ok=True)

state = {}
last_id = {}

def inspect(img):
    p = subprocess.run(
        ["docker", "image", "inspect", img, "--format", "{{.Id}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if p.returncode == 0:
        return True, p.stdout.strip()
    return False, None

new_file = not OUT.exists()

with OUT.open("a", newline="", buffering=1) as f:
    w = csv.writer(f)

    if new_file:
        w.writerow([
            "time",
            "event",
            "image",
            "image_id",
        ])

    while True:
        ready = 0

        for img in IMAGES:
            exists, image_id = inspect(img)

            if exists:
                ready += 1
                last_id[img] = image_id

            old = state.get(img)

            if old is None:
                state[img] = exists
                if exists:
                    w.writerow([
                        datetime.now().isoformat(),
                        "INITIAL_PRESENT",
                        img,
                        image_id,
                    ])

            elif old and not exists:
                w.writerow([
                    datetime.now().isoformat(),
                    "LOST",
                    img,
                    last_id.get(img, ""),
                ])
                print(
                    f"[LOST] {datetime.now()} "
                    f"{img} "
                    f"{last_id.get(img, '')}",
                    flush=True,
                )
                state[img] = False

            elif not old and exists:
                w.writerow([
                    datetime.now().isoformat(),
                    "APPEARED",
                    img,
                    image_id,
                ])
                print(
                    f"[APPEARED] {datetime.now()} "
                    f"{img} {image_id}",
                    flush=True,
                )
                state[img] = True

        print(
            f"[{datetime.now()}] READY={ready}/{len(IMAGES)}",
            flush=True,
        )

        time.sleep(5)
