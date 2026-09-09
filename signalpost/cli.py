"""Command line: run / validate / site."""
from __future__ import annotations

import argparse
import gzip
import json
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .models import validate_envelope
from .net import Session, utc_now


def read_inputs(path: Path) -> list[str]:
    orgs: list[str] = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                cand = obj.get("organisation_number") or obj.get("organisasjonsnummer") or obj.get("org")
            except ValueError:
                cand = None
        else:
            cand = line.split(",")[0]
        digits = re.sub(r"\D", "", str(cand or ""))
        if not re.fullmatch(r"\d{9}", digits):
            print(f"warning: skipping unparseable input line: {line[:60]!r}", file=sys.stderr)
            continue
        if digits in seen:
            print(f"warning: duplicate organisation number {digits} ignored", file=sys.stderr)
            continue
        seen.add(digits)
        orgs.append(digits)
    return orgs


def load_universe(path: Path, wanted: set[str]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not path or not path.exists():
        return rows
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            i = line.find('"organisation_number":"')
            if i < 0:
                continue
            org = line[i + 23:i + 32]
            if org in wanted:
                rows[org] = json.loads(line)
                if len(rows) == len(wanted):
                    break
    return rows


def cmd_run(a: argparse.Namespace) -> int:
    from . import refresh
    from .pipeline import failed_envelope, process_company

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    orgs = read_inputs(Path(a.input))
    if a.expected_count and len(orgs) != a.expected_count:
        print(f"error: input has {len(orgs)} organisation numbers, expected {a.expected_count}", file=sys.stderr)
        return 2
    universe = load_universe(Path(a.universe), set(orgs)) if a.universe else {}
    previous = refresh.load_previous(Path(a.previous)) if a.previous else {}
    session = Session(out, max_total_requests=a.max_requests, per_company_cap=a.per_company_cap, timeout=a.timeout)
    started = utc_now()
    t0 = time.monotonic()
    nav_index = None
    if not a.no_nav_feed:
        from .navfeed import FeedIndex
        nav_index = FeedIndex(session, days=a.nav_days)
        ok = nav_index.build()
        print(f"nav feed index: built={ok} {json.dumps(nav_index.summary())} requests={session.total_requests} elapsed={int(time.monotonic() - t0)}s", file=sys.stderr)
    checkpoint = out / "checkpoint.jsonl"
    done: dict[str, dict] = {}
    if a.resume and checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            if line.strip():
                env = json.loads(line)
                done[env["organisation_number"]] = env
        print(f"resuming: {len(done)} envelopes already checkpointed", file=sys.stderr)
    lock = threading.Lock()
    ck = open(checkpoint, "a", encoding="utf-8")

    def work(org: str) -> dict:
        s = utc_now()
        try:
            env = process_company(org, universe.get(org), session, a.run_id, previous.get(org), started, nav_index=nav_index)
        except Exception as exc:  # pragma: no cover - last line of defence
            env = failed_envelope(org, a.run_id, s, f"{type(exc).__name__}: {exc}")
        with lock:
            ck.write(json.dumps(env, ensure_ascii=False) + "\n")
            ck.flush()
        return env

    todo = [o for o in orgs if o not in done]
    n = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(work, o): o for o in todo}
        for fut in as_completed(futures):
            env = fut.result()
            done[env["organisation_number"]] = env
            n += 1
            if n % 10 == 0 or n == len(todo):
                print(f"[{n}/{len(todo)}] requests={session.total_requests} elapsed={int(time.monotonic() - t0)}s", file=sys.stderr)
    ck.close()
    envelopes = [done[o] for o in orgs]
    with open(out / "envelopes.jsonl", "w", encoding="utf-8") as fh:
        for env in envelopes:
            fh.write(json.dumps(env, ensure_ascii=False) + "\n")
    (out / "manifest.txt").write_text("\n".join(orgs) + "\n", encoding="utf-8")
    session.dump_log()
    report = build_report(envelopes, session, a.run_id, started, previous, t0)
    report["nav_feed"] = nav_index.summary() if nav_index is not None else {"disabled": True}
    (out / "run-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("inputs", "envelopes", "requests", "runtime_ms", "terminal_status_counts", "section_state_counts")}, ensure_ascii=False), file=sys.stderr)
    return 0 if report["envelopes"] == report["inputs"] else 1


def build_report(envelopes: list[dict], session: Session, run_id: str, started: str, previous: dict, t0: float) -> dict:
    from collections import Counter
    ts = Counter(e["run"]["terminal_status"] for e in envelopes)
    sec: dict[str, Counter] = {}
    for e in envelopes:
        for k, v in e.get("sections", {}).items():
            sec.setdefault(k, Counter())[v] += 1
    lat = sorted(e["operations"]["runtime_ms"] for e in envelopes) or [0]
    changes = Counter(c["change_type"] for e in envelopes for c in e.get("changes", []))
    fields = Counter(c["field"] for e in envelopes for c in e.get("claims", []) if c["availability"] == "available")
    return {"run_id": run_id, "started_at": started, "completed_at": utc_now(), "agent_version": envelopes[0]["run"]["agent_version"] if envelopes else None,
            "inputs": len(envelopes), "envelopes": len(envelopes), "terminal_status_counts": dict(ts),
            "section_state_counts": {k: dict(v) for k, v in sec.items()}, "available_claims_by_field": dict(fields),
            "requests": session.total_requests, "bytes": session.total_bytes, "third_party_cost_usd": 0.0,
            "runtime_ms": int((time.monotonic() - t0) * 1000), "p50_ms": int(statistics.median(lat)), "p95_ms": int(lat[max(0, int(len(lat) * 0.95) - 1)]),
            "budget_exhausted_companies": [e["organisation_number"] for e in envelopes if e["operations"].get("budget_exhausted")],
            "previous_run_id": next(iter(previous.values()))["run"]["run_id"] if previous else None,
            "changes_by_type": dict(changes), "validation_problems": sum(1 for e in envelopes for er in e.get("errors", []) if er.get("stage") == "validation")}


def cmd_validate(a: argparse.Namespace) -> int:
    path = Path(a.envelopes)
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    problems = 0
    orgs = [r.get("organisation_number") for r in rows]
    if len(set(orgs)) != len(orgs):
        print("error: duplicate organisation numbers in envelopes", file=sys.stderr)
        problems += 1
    if a.expected_count and len(rows) != a.expected_count:
        print(f"error: {len(rows)} envelopes, expected {a.expected_count}", file=sys.stderr)
        problems += 1
    if a.manifest:
        manifest = [l.strip() for l in Path(a.manifest).read_text(encoding="utf-8").splitlines() if l.strip()]
        if manifest != orgs:
            print("error: manifest order/membership differs from envelopes", file=sys.stderr)
            problems += 1
    for r in rows:
        p = validate_envelope(r)
        if p:
            problems += 1
            print(f"{r.get('organisation_number')}: {p[0][:200]}", file=sys.stderr)
    print(f"validated {len(rows)} envelopes, {problems} problem(s)")
    return 1 if problems else 0


def cmd_site(a: argparse.Namespace) -> int:
    import runpy
    sys.argv = ["build_site.py", "--envelopes", a.envelopes, "--out", a.out]
    runpy.run_path(str(Path(__file__).resolve().parent.parent / "tools" / "build_site.py"), run_name="__main__")
    return 0


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="signalpost", description="Norwegian organisation number -> evidence-backed company profile")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process a batch of organisation numbers")
    r.add_argument("--input", required=True, help="JSONL/JSON-lines or text file with organisation numbers")
    r.add_argument("--universe", default="", help="signalpost-company-universe-2025.jsonl.gz (frozen registry rows)")
    r.add_argument("--out", required=True, help="output directory (envelopes.jsonl, run-report.json, snapshots/)")
    r.add_argument("--run-id", required=True)
    r.add_argument("--previous", default="", help="previous run's envelopes.jsonl for refresh diffing")
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--max-requests", type=int, default=1950)
    r.add_argument("--per-company-cap", type=int, default=26)
    r.add_argument("--timeout", type=float, default=12.0)
    r.add_argument("--expected-count", type=int, default=0)
    r.add_argument("--resume", action="store_true", help="reuse envelopes already in <out>/checkpoint.jsonl")
    r.add_argument("--nav-days", type=int, default=60, help="how many days of the NAV job feed to scan for active ads")
    r.add_argument("--no-nav-feed", action="store_true", help="skip the NAV feed scan and use the search-API fallback")
    r.set_defaults(fn=cmd_run)
    v = sub.add_parser("validate", help="validate an envelopes.jsonl against the contract")
    v.add_argument("--envelopes", required=True)
    v.add_argument("--expected-count", type=int, default=0)
    v.add_argument("--manifest", default="")
    v.set_defaults(fn=cmd_validate)
    s = sub.add_parser("site", help="build the static results site")
    s.add_argument("--envelopes", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_site)
    a = p.parse_args(argv)
    sys.exit(a.fn(a))
