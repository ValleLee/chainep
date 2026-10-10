#!/usr/bin/env bash
set -uo pipefail

ROOT=/lg-home/cog8/lwli6/ChainSweExp/ChainSWE
EXPERIMENT=experiments/0907test_xh_on30
OUTPUT="$ROOT/$EXPERIMENT/output"

cd "$ROOT" || exit 1

python3 "$EXPERIMENT/run_0907_on30.py" \
  --chain-list "$EXPERIMENT/chain_list_30.txt" \
  --model-config "$EXPERIMENT/models_xh.json" \
  --model xh-model \
  --env-file ../SWE-Edit/openai_chat.env \
  --uv-bin "$EXPERIMENT/tools/bin/uv" \
  --run-name output \
  --out-root "$EXPERIMENT" \
  --modes oracle seq seq_mem \
  --parallel-chains 5 \
  > "$OUTPUT/launcher.log" 2>&1
launch_rc=$?

if find "$OUTPUT/chain_runs" -name chain_result.json -print -quit 2>/dev/null | grep -q .; then
  python3 "$EXPERIMENT/summarize_0907_metrics.py" \
    --run-dir "$OUTPUT" \
    > "$OUTPUT/summarize.log" 2>&1
  summary_rc=$?
else
  printf '%s\n' "No chain results were available to summarize (launcher rc=$launch_rc)." \
    > "$OUTPUT/summarize.log"
  summary_rc=0
fi

if (( launch_rc != 0 )); then
  exit "$launch_rc"
fi
exit "$summary_rc"
