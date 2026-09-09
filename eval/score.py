"""Local scorer: the fast feedback signal between independent reviews. Never a substitute for the hidden score.

    python eval/score.py --envelopes out/envelopes.jsonl [--gold eval/gold.jsonl] [--report eval/report.json]

Reports, per field family, how many companies have at least one ``available`` claim (company coverage) and
how many claims exist in total (claim volume), the evidence-completeness rate, the availability-state mix,
website gate verdicts, budget use and latency. With a gold file (rows: organisation_number, official_website,
optional wrong_domains[]) it also reports exact-website precision/recall and wrong-company publications.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from urllib.parse import urlsplit

FAMILIES = {
    "official_website": ["official_website"],
    "social_profile": ["social_profile"],
    "contact": ["contact_email", "contact_phone", "registry_email", "registry_phone"],
    "site_leadership": ["site_leader"],
    "site_locations": ["site_location"],
    "jobs": ["job_posting", "careers_page"],
    "dated_activity": ["news_item", "sitemap_lastmod", "registry_update"],
    "accounts": ["revenue", "annual_result"],
    "roles": ["role"],
    "workplaces": ["workplace"],
    "description": ["website_description", "public_brand"],
}


def host(u: str | None) -> str | None:
    if not u:
        return None
    h = (urlsplit(u if "://" in u else "https://" + u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--envelopes", required=True)
    ap.add_argument("--gold", default="")
    ap.add_argument("--report", default="")
    a = ap.parse_args()
    envs = [json.loads(l) for l in open(a.envelopes, encoding="utf-8") if l.strip()]
    n = len(envs)
    fam_company = Counter()
    fam_claims = Counter()
    states = Counter()
    ev_missing = 0
    ev_total = 0
    verdicts = Counter()
    for e in envs:
        ev_ids = {v["id"] for v in e["evidence"]}
        seen = set()
        for c in e["claims"]:
            states[c["availability"]] += 1
            if c["availability"] == "available":
                ev_total += 1
                if not c["evidence_ids"] or any(i not in ev_ids for i in c["evidence_ids"]):
                    ev_missing += 1
            for fam, fields in FAMILIES.items():
                if c["field"] in fields and c["availability"] == "available":
                    fam_claims[fam] += 1
                    if fam not in seen:
                        seen.add(fam)
                        fam_company[fam] += 1
        verdicts[e["sections"].get("web")] += 1
    lat = sorted(e["operations"]["runtime_ms"] for e in envs) or [0]
    reqs = [e["operations"]["requests"] for e in envs]
    report = {
        "companies": n,
        "terminal_status": dict(Counter(e["run"]["terminal_status"] for e in envs)),
        "family_company_coverage": {k: round(fam_company[k] / n, 3) for k in FAMILIES} if n else {},
        "family_claim_volume": dict(fam_claims),
        "availability_states": dict(states),
        "evidence_completeness": round(1 - ev_missing / ev_total, 4) if ev_total else None,
        "web_section_states": dict(verdicts),
        "requests_total": sum(reqs), "requests_mean": round(statistics.mean(reqs), 1) if reqs else 0, "requests_max": max(reqs) if reqs else 0,
        "runtime_p50_ms": int(statistics.median(lat)), "runtime_p95_ms": int(lat[max(0, int(len(lat) * 0.95) - 1)]),
        "changes_by_type": dict(Counter(c["change_type"] for e in envs for c in e.get("changes", []))),
    }
    if a.gold:
        gold = {r["organisation_number"]: r for r in (json.loads(l) for l in open(a.gold, encoding="utf-8") if l.strip())}
        tp = fp = fn = wrong = 0
        for e in envs:
            g = gold.get(e["organisation_number"])
            if not g:
                continue
            pub = host(e["identity"].get("official_website"))
            want = host(g.get("official_website"))
            if pub and want and pub == want:
                tp += 1
            elif pub and (not want or pub != want):
                fp += 1
                if pub in {host(w) for w in g.get("wrong_domains", [])} or (want and pub != want):
                    wrong += 1
            elif not pub and want:
                fn += 1
        report["gold"] = {"rows": len(gold), "website_precision": round(tp / (tp + fp), 3) if tp + fp else None,
                          "website_recall": round(tp / (tp + fn), 3) if tp + fn else None, "wrong_company_publications": wrong}
    print(json.dumps(report, indent=2))
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)


if __name__ == "__main__":
    main()
