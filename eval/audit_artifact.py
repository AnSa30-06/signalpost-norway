"""Evidence-integrity audit of a run directory, aimed at the faults in Builderr's first report.

    python eval/audit_artifact.py --run out/run-final3

Part 1, every evidence record: its ``claim_span`` must be verbatim in its snapshot (raw bytes, entity-decoded
bytes, or the visible text of an HTML page; whitespace collapsed on both sides). Two computed counts are exempt
(``nav_feed_scan`` and ``role_count``, see docs/DATA_SCHEMA.md), as is evidence used only by claims that are not
``available`` (an "HTTP 404" or an absent field is the proof of absence).

Part 2, every published website, from the stored homepage and nothing else:
1. the identity proof span is verbatim on the stored page;
2. when the reason is the organisation number, that number is on the page; otherwise the page states no *other*
   labelled organisation number (the sister-company trap);
3. the page's title does not introduce a different registered company (the holding-company trap);
4. the page does not name three or more registered companies without naming us in its title (the listing trap);
5. the published ``public_brand`` shares a token with the legal name (the wrong-brand trap);
6. the current gate, re-run on the stored page, still says ``exact``.

Part 3, every profile: the ``answers`` block exists and no ``available`` claim lacks evidence.
Exit code is 1 when anything fails, so it can gate a submission.
"""
from __future__ import annotations

import argparse
import collections
import html as H
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from signalpost import identity  # noqa: E402
from signalpost.net import FetchResult  # noqa: E402

COMPUTED_METHODS = {"nav_feed_scan", "universe_row_fallback"}
COMPUTED_FIELDS = {"role_count", "active_job_count"}


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


class Snapshots:
    def __init__(self, root: Path):
        self.root, self.cache = root, {}

    def forms(self, path: str):
        """(raw, entity-decoded raw, visible text) of a snapshot, whitespace removed; None when missing."""
        if path in self.cache:
            return self.cache[path]
        p = self.root / path
        if not p.is_file():
            self.cache[path] = None
            return None
        raw = p.read_bytes().decode("utf-8", "replace")
        forms = [squash(raw), squash(H.unescape(raw))]
        if path.endswith(".html"):
            forms.append(squash(identity.html_text(raw)))
        self.cache[path] = tuple(forms)
        return self.cache[path]


def audit_evidence(envs, snaps: Snapshots, failures: list[str]) -> dict:
    checked = collections.Counter()
    bad = collections.Counter()
    for e in envs:
        org = e["organisation_number"]
        used_by = collections.defaultdict(list)
        for c in e["claims"]:
            for eid in c["evidence_ids"]:
                used_by[eid].append(c)
        for v in e["evidence"]:
            m, p = v.get("extraction_method"), v.get("snapshot_path") or ""
            claims = used_by.get(v["id"], [])
            if m in COMPUTED_METHODS or (claims and all(c["field"] in COMPUTED_FIELDS for c in claims)):
                continue
            if claims and not any(c["availability"] == "available" for c in claims):
                continue
            checked[m] += 1
            forms = snaps.forms(p) if p else None
            if forms is None:
                bad[m] += 1
                failures.append(f"{org}: evidence {v['id']} ({m}) has no snapshot on disk")
                continue
            s = squash(v.get("claim_span") or "")
            if not s or not any(s in f for f in forms):
                bad[m] += 1
                if bad[m] <= 3:
                    failures.append(f"{org}: evidence {v['id']} ({m}) span not verbatim in snapshot: {(v.get('claim_span') or '')[:70]!r}")
    return {"checked": sum(checked.values()), "bad": sum(bad.values()),
            "by_method": {m: (bad[m], checked[m]) for m in checked if bad[m]}}


def audit_websites(envs, root: Path, failures: list[str]) -> int:
    published = 0
    for e in envs:
        org, name = e["organisation_number"], e["identity"].get("legal_name") or ""
        if e["sections"].get("web") != "available":
            continue
        published += 1
        ev = next((v for v in e["evidence"] if v.get("extraction_method") == "identity_gate"), None)
        snap = root / (ev or {}).get("snapshot_path", "")
        if not ev or not snap.is_file():
            failures.append(f"{org} {name[:30]}: no identity snapshot on disk")
            continue
        html = snap.read_bytes().decode("utf-8", "replace")
        text = identity.html_text(html)
        s = squash(ev["claim_span"])
        if not s or not (s in squash(text) or s in squash(html) or s in squash(H.unescape(html))):
            failures.append(f"{org} {name[:30]}: identity span not verbatim on the stored page: {ev['claim_span'][:60]!r}")
        reasons = (e["identity"].get("website_identity") or {}).get("reasons") or []
        pat = identity._org_pattern(org)
        if reasons and reasons[0] == "org_number_on_page":
            if not (pat and pat.search(text)):
                failures.append(f"{org} {name[:30]}: reason says org number on page, but it is not")
        else:
            others = identity.other_org_numbers(text, org)
            if others:
                failures.append(f"{org} {name[:30]}: page states another organisation number {others[0]}")
        soup_title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", soup_title.group(1)).strip() if soup_title else ""
        ba = next((c["value"] for c in e["claims"] if c["field"] == "business_address" and c["availability"] == "available"), None) or {}
        former = next((c["value"] for c in e["claims"] if c["field"] == "former_names" and c["availability"] == "available"), []) or []
        email = next((c["value"] for c in e["claims"] if c["field"] == "registry_email" and c["availability"] == "available"), None)
        prof = {"organisation_number": org, "name": name, "municipality": e["identity"].get("municipality"),
                "registry": {"forretningsadresse": {"adresse": [ba.get("street") or ""], "postnummer": ba.get("postcode"),
                                                    "poststed": ba.get("city"), "kommune": ba.get("municipality")},
                             "historiskeNavn": [{"navn": n} for n in former], "epostadresse": email}}
        if identity.title_names_another_entity([title], prof):
            failures.append(f"{org} {name[:30]}: title introduces another entity: {title[:60]!r}")
        want = set(identity.name_tokens(name))
        companies = identity.registered_company_names(text)
        if len(companies) >= 3 and not (set(identity.name_tokens(title)) & want):
            failures.append(f"{org} {name[:30]}: page names {len(companies)} companies and the title is not ours")
        brand = e["identity"].get("public_brand")
        if brand and not (set(identity.tokens(brand)) & want):
            failures.append(f"{org} {name[:30]}: brand {brand!r} shares no token with the legal name")
        fr = FetchResult(url=ev["source_url"], final_url=ev.get("final_url") or ev["source_url"], status=200,
                         body=html.encode(), text=html, retrieved_at=ev["retrieved_at"], sha256=ev.get("content_sha256") or "",
                         snapshot_path=ev["snapshot_path"], content_type="text/html")
        verdict = identity.assess(prof, fr)
        if verdict["status"] != "exact":
            failures.append(f"{org} {name[:30]}: current gate no longer says exact ({verdict['reasons'][:2]})")
    return published


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    root = Path(a.run)
    envs = [json.loads(l) for l in open(root / "envelopes.jsonl", encoding="utf-8") if l.strip()]
    failures: list[str] = []
    for e in envs:
        org, name = e["organisation_number"], e["identity"].get("legal_name") or ""
        ev_ids = {v["id"] for v in e["evidence"]}
        for c in e["claims"]:
            if c["availability"] == "available" and (not c["evidence_ids"] or any(i not in ev_ids for i in c["evidence_ids"])):
                failures.append(f"{org} {name[:30]}: available claim {c['field']} without evidence")
        if not isinstance(e.get("synthesis", {}).get("answers"), list) or len(e["synthesis"]["answers"]) < 10:
            failures.append(f"{org} {name[:30]}: answers block missing")
    ev = audit_evidence(envs, Snapshots(root), failures)
    published = audit_websites(envs, root, failures)
    print(f"profiles: {len(envs)} | evidence records checked: {ev['checked']} | non-verbatim spans: {ev['bad']}"
          f" | published websites audited: {published} | failures: {len(failures)}")
    for m, (b, n) in sorted(ev["by_method"].items(), key=lambda kv: -kv[1][0]):
        print(f"  {m:40s} {b:6d} / {n:6d} not verbatim")
    for f in failures[:40]:
        print("  FAIL", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
