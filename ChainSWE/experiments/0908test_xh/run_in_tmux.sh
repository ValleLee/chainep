#!/usr/bin/env bash
set -uo pipefail

ROOT=/lg-home/cog8/lwli6/ChainSweExp/ChainSWE
EXPERIMENT=experiments/0908test_xh
OUTPUT="$ROOT/$EXPERIMENT/output"
REFERENCE=experiments/0907test_xh_on30/run_0907_on30.py
SUMMARY="$ROOT/scripts/tool/summarize_0908_xh.py"

cd "$ROOT" || exit 1
mkdir -p "$OUTPUT"

python3 "$ROOT/$REFERENCE" \
  --chain-list "$ROOT/$EXPERIMENT/chain_list_30.txt" \
  --model-config "$ROOT/$EXPERIMENT/models_xh.json" \
  --model xh-model \
  --env-file ../SWE-Edit/openai_chat.env \
  --uv-bin "$ROOT/experiments/0907test_xh_on30/tools/bin/uv" \
  --run-name output \
  --out-root "$ROOT/$EXPERIMENT" \
  --modes oracle seq seq_mem \
  --parallel-chains 5 \
  > "$OUTPUT/launcher.log" 2>&1
launch_rc=$?

if find "$OUTPUT/chain_runs" -name chain_result.json -print -quit 2>/dev/null | grep -q .; then
  python3 "$SUMMARY" --run-dir "$OUTPUT" > "$OUTPUT/summarize.log" 2>&1
  summary_rc=$?
else
  printf '%s\n' "No chain results were available to summarize (launcher rc=$launch_rc)." > "$OUTPUT/summarize.log"
  summary_rc=0
fi

if (( launch_rc != 0 )); then
  exit "$launch_rc"
fi
exit "$summary_rc"
