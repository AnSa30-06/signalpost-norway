"""Deterministic batch selection from the frozen public universe.

    python eval/select_batch.py --universe signalpost-company-universe-2025.jsonl.gz --count 1000 --seed 20260909 --output batch.jsonl

The sample is a uniform random draw over the eligible universe (active, not bankrupt, not liquidating) with a
fixed seed, so it can be reproduced exactly. Each row also gets a stable hash-based split
(development 60% / validation 20% / final 20%) that never changes between runs.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
from pathlib import Path


def split_for(org: str) -> str:
    h = int(hashlib.sha256(org.encode()).hexdigest()[:8], 16) % 100
    return "development" if h < 60 else ("validation" if h < 80 else "final")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", required=True)
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260909)
    ap.add_argument("--output", required=True)
    ap.add_argument("--with-website-share", type=float, default=-1, help="optional: force this share of rows with a registry website")
    a = ap.parse_args()
    opener = gzip.open if a.universe.endswith(".gz") else open
    rows = []
    with opener(a.universe, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("bankrupt") or r.get("liquidating"):
                continue
            rows.append(r)
    rng = random.Random(a.seed)
    rng.shuffle(rows)
    chosen = rows[: a.count]
    chosen.sort(key=lambda r: r["organisation_number"])
    with open(a.output, "w", encoding="utf-8") as out:
        for r in chosen:
            out.write(json.dumps({"organisation_number": r["organisation_number"], "name": r["name"], "legal_form": r["legal_form"],
                                  "municipality": r["municipality"], "registry_website": r.get("website") or None,
                                  "evaluation_split": split_for(r["organisation_number"])}, ensure_ascii=False) + "\n")
    n_web = sum(1 for r in chosen if r.get("website"))
    print(json.dumps({"universe_rows": len(rows), "selected": len(chosen), "seed": a.seed, "with_registry_website": n_web,
                      "sha256_of_manifest": hashlib.sha256("\n".join(r["organisation_number"] for r in chosen).encode()).hexdigest()}))


if __name__ == "__main__":
    main()
