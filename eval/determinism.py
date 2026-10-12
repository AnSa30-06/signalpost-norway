"""Compare two runs of the same input: which companies' facts differ, and in which fields.

    python eval/determinism.py out/det-A/envelopes.jsonl out/det-B/envelopes.jsonl

Builderr's check (2026-10-02): the same input must give the same factual output. This compares, per company,
the set of (field, availability, value) over every claim, and the section states. Retrieval times, hashes,
spans, ids, run metadata and synthesis wording are left out: they describe the fetch, not the fact.
Exit code 1 when any company differs.
"""
from __future__ import annotations

import json
import sys
from collections import Counter


def facts(env: dict) -> set:
    out = set()
    for c in env.get("claims", []):
        out.add((c.get("field"), c.get("availability"), json.dumps(c.get("value"), sort_keys=True, ensure_ascii=False)))
    for k, v in (env.get("sections") or {}).items():
        out.add(("section:" + k, v, ""))
    return out


def load(path: str) -> dict:
    return {e["organisation_number"]: e for e in (json.loads(l) for l in open(path, encoding="utf-8") if l.strip())}


def main() -> int:
    a, b = load(sys.argv[1]), load(sys.argv[2])
    differ, fields = [], Counter()
    for org in sorted(set(a) | set(b)):
        if org not in a or org not in b:
            differ.append((org, ["missing in one run"]))
            continue
        fa, fb = facts(a[org]), facts(b[org])
        if fa != fb:
            changed = sorted({f[0] for f in fa ^ fb})
            fields.update(changed)
            differ.append((org, changed))
    print(f"companies: {len(set(a) | set(b))} | differing: {len(differ)}")
    for f, n in fields.most_common():
        print(f"  {f:32s} {n}")
    for org, changed in differ[:40]:
        print("  ", org, (a.get(org) or b.get(org))["identity"].get("legal_name"), changed)
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
