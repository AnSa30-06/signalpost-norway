#!/usr/bin/env bash
# The one evaluator command.
#   ./run.sh <input.jsonl|input.txt> <out_dir> [previous_envelopes.jsonl]
# Installs pinned dependencies, runs the batch, then validates that there is exactly one envelope per input.
# Optional environment: SIGNALPOST_UNIVERSE, SIGNALPOST_RUN_ID, SIGNALPOST_WORKERS, BRAVE_API_KEY.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <input.jsonl|input.txt> <out_dir> [previous_envelopes.jsonl]" >&2
  exit 2
fi

INPUT="$1"
OUT="$2"
PREV="${3:-}"
UNIVERSE="${SIGNALPOST_UNIVERSE:-signalpost-company-universe-2025.jsonl.gz}"
RUN_ID="${SIGNALPOST_RUN_ID:-$(basename "$OUT")}"
WORKERS="${SIGNALPOST_WORKERS:-8}"

if [ ! -f "$INPUT" ]; then echo "input not found: $INPUT" >&2; exit 2; fi
if [ ! -f "$UNIVERSE" ]; then echo "universe file not found: $UNIVERSE (set SIGNALPOST_UNIVERSE)" >&2; exit 2; fi

# Number of non-empty input lines = number of envelopes the run must produce.
COUNT="$(grep -c . "$INPUT")"

uv sync --frozen

ARGS=(--input "$INPUT" --universe "$UNIVERSE" --out "$OUT" --run-id "$RUN_ID"
      --workers "$WORKERS" --max-requests 1950 --per-company-cap 22 --expected-count "$COUNT")
if [ -n "$PREV" ]; then
  if [ ! -f "$PREV" ]; then echo "previous envelopes not found: $PREV" >&2; exit 2; fi
  ARGS+=(--previous "$PREV")
fi

uv run --frozen python -m signalpost run "${ARGS[@]}"
uv run --frozen python -m signalpost validate --envelopes "$OUT/envelopes.jsonl" --expected-count "$COUNT"

echo "done: $OUT/envelopes.jsonl ($COUNT envelopes), report in $OUT/run-report.json"
