#!/usr/bin/env bash
set -u

IMG="$1"

ROOT="/lg-home/cog8/lwli6/chain"
LOG_DIR="$ROOT/ChainSWE/logs/docker_pull"
mkdir -p "$LOG_DIR"

SAFE_NAME=$(echo "$IMG" | sed 's#[/:]#_#g')
LOG="$LOG_DIR/${SAFE_NAME}.log"

HASH=$(printf '%s' "$IMG" | sha256sum | cut -c1-16)
PIN="chainswe-pin-$HASH"

ensure_pin() {
    # 已经存在且正在运行：直接完成
    if docker container inspect "$PIN" >/dev/null 2>&1; then
        RUNNING=$(docker container inspect \
            --format '{{.State.Running}}' "$PIN" 2>/dev/null)

        if [ "$RUNNING" = "true" ]; then
            echo "[PIN OK] $IMG -> $PIN"
            return 0
        fi

        # 旧 pin 如果只是 Created/Exited，删掉后重建成 running pin
        echo "[RECREATE PIN] $PIN"
        docker rm -f "$PIN" >/dev/null 2>&1 || true
    fi

    echo "[PIN START] $IMG -> $PIN"

    docker run -d \
        --name "$PIN" \
        --label "chainswe.pin=1" \
        --label "chainswe.image=$IMG" \
        --network none \
        --read-only \
        --restart unless-stopped \
        --entrypoint /bin/sh \
        "$IMG" \
        -c 'while :; do sleep 3600; done' \
        >/dev/null

    sleep 1

    RUNNING=$(docker container inspect \
        --format '{{.State.Running}}' "$PIN" 2>/dev/null || echo false)

    if [ "$RUNNING" = "true" ]; then
        echo "[PINNED] $IMG"
        return 0
    fi

    echo "[PIN FAILED] $IMG"
    return 1
}


# ---------- image 已经存在 ----------
if docker image inspect "$IMG" >/dev/null 2>&1; then
    echo "[IMAGE EXISTS] $IMG"

    if ensure_pin; then
        echo "[READY] $IMG"
        exit 0
    else
        exit 1
    fi
fi


# ---------- image 不存在：下载 ----------
for attempt in 1 2 3; do
    echo "[PULL attempt=$attempt] $IMG"

    if docker pull "$IMG" >"$LOG" 2>&1; then
        echo "[PULL OK] $IMG"

        if ensure_pin; then
            echo "[READY] $IMG"
            exit 0
        fi
    fi

    echo "[RETRY] $IMG"
    sleep $((attempt * 10))
done

echo "[FAILED] $IMG"
exit 1
