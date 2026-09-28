#!/usr/bin/env bash
# The one evaluator command.
#   ./run.sh <input.jsonl|input.txt> <out_dir> [previous_envelopes.jsonl]
# Installs pinned dependencies, runs the batch, then validates that there is exactly one envelope per input.
# Optional environment: SIGNALPOST_UNIVERSE, SIGNALPOST_RUN_ID, SIGNALPOST_WORKERS, SIGNALPOST_TIME_BUDGET_MIN,
# SIGNALPOST_MAX_REQUESTS, BRAVE_SEARCH_API_KEY (Brave Search API: website discovery when the registry has none).
# A local .env file (git-ignored, see .env.example) is read too; a variable already set wins.
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
# 24 workers: the registry's filing-years endpoint is paced at ~28 requests a minute for the whole process, so
# throughput stops rising above ~20 workers; 8 workers took ~95 minutes per 1,000 companies.
WORKERS="${SIGNALPOST_WORKERS:-24}"
# The run always finishes inside this budget: near the end, search and deeper crawls are skipped and every
# company still gets a terminal envelope. Official batches are 1,000+ companies with a fixed time budget.
TIME_BUDGET_MIN="${SIGNALPOST_TIME_BUDGET_MIN:-40}"

if [ ! -f "$INPUT" ]; then echo "input not found: $INPUT" >&2; exit 2; fi

# The universe file is an optional seed, not a dependency: it pre-fills legal name, form, municipality and
# industry before the first request. Without it the agent reads all of that from the live registry instead.
# Missing it must never stop the run, because a fresh clone does not carry the 12 MB archive.
if [ ! -f "$UNIVERSE" ]; then
  echo "note: universe file not found at $UNIVERSE; continuing without it (values come from the live registry)." >&2
  echo "      to use it: curl -LO https://builderr.ai/signalpost-company-universe-2025.jsonl.gz  (or set SIGNALPOST_UNIVERSE)" >&2
  UNIVERSE=""
fi

# Number of non-empty input lines = number of envelopes the run must produce.
COUNT="$(grep -c . "$INPUT")"
# The request cap scales with the batch (it was a fixed 1,950 when the official batch was 100 companies).
MAX_REQUESTS="${SIGNALPOST_MAX_REQUESTS:-$(( COUNT * 30 ))}"

uv sync --frozen

ARGS=(--input "$INPUT" --universe "$UNIVERSE" --out "$OUT" --run-id "$RUN_ID"
      --workers "$WORKERS" --max-requests "$MAX_REQUESTS" --per-company-cap 30 --expected-count "$COUNT"
      --time-budget-min "$TIME_BUDGET_MIN")
if [ -n "$PREV" ]; then
  if [ ! -f "$PREV" ]; then echo "previous envelopes not found: $PREV" >&2; exit 2; fi
  ARGS+=(--previous "$PREV")
fi

uv run --frozen python -m signalpost run "${ARGS[@]}"
uv run --frozen python -m signalpost validate --envelopes "$OUT/envelopes.jsonl" --expected-count "$COUNT"

echo "done: $OUT/envelopes.jsonl ($COUNT envelopes), report in $OUT/run-report.json"
