"""NAV job vacancy feed connector (official, token-based, exact org-number confirmation).

Why this exists: the public search API on arbeidsplassen.nav.no answers HTTP 429 to crawlers after a small burst
and carries no organisation number. The official feed (https://navikt.github.io/pam-stilling-feed/) is paginated
by time, needs a bearer token (a public experimentation token is published at /api/publicToken; a private token
comes from NAV on request and is passed through the NAV_FEED_TOKEN environment variable), and every ad's detail
record carries ``employer.orgnr``.

Per run: scan the last ``days`` of feed events once (three time-window chains fetched concurrently, one request
per 1,000 events), keep the latest event per ad, and index active ads by folded employer name. Per company:
pick ads whose employer name matches the legal name, fetch only those ad records, and publish an ad only when
``employer.orgnr`` equals the organisation number. Terms (arbeidsplassen.nav.no/vilkar-api): anyone may use the
service, ads must not be shown once inactive, and contact details must be handled under GDPR; this connector
publishes title, dates, location and the public ad link only, never contacts.
"""
from __future__ import annotations

import email.utils
import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .models import AVAILABLE, BLOCKED, FAILED, IdGen, new_claim, new_error, new_evidence

BASE = "https://pam-stilling-feed.nav.no"
FEED_URL = BASE + "/api/v1/feed"
TOKEN_URL = BASE + "/api/publicToken"
SHARED_KEY = "_navfeed"          # session budget key: exempt from the per-company cap, counted in the global cap
MAX_PAGES = 150                  # hard stop per chain
MAX_CANDIDATES = 8               # ad records fetched per company at most
FEED_TIMEOUT = 40.0              # a feed page carries 1,000 events and can exceed a site-crawl timeout
LEGAL_FORMS = {"as", "asa", "ans", "da", "enk", "nuf", "sa", "ba", "ks", "sti", "brl", "iks", "kf", "fli", "esek", "sam", "spa"}
GENERIC = {"og", "and", "the", "group", "gruppen", "norge", "norway", "as", "avd", "avdeling", "enhet", "for"}


def fold(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", (s or "")).casefold().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def name_tokens(name: Optional[str]) -> list[str]:
    return [t for t in fold(name).split() if t not in LEGAL_FORMS and t not in GENERIC and len(t) >= 2]


class FeedIndex:
    """Active NAV ads from the last ``days`` days, indexed by folded employer name."""

    def __init__(self, session, days: int = 60, chains: int = 3, token: Optional[str] = None):
        self.s = session
        self.days = days
        self.chains = max(1, chains)
        self.token = token or os.environ.get("NAV_FEED_TOKEN") or None
        self.ads: dict[str, dict] = {}
        self.by_name: dict[str, list[str]] = {}
        self.pages = 0
        self.errors: list[str] = []
        self.built = False
        self.built_at: Optional[str] = None
        self._lock = threading.Lock()

    # ---- build -------------------------------------------------------------------------------------------
    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        if extra:
            h.update(extra)
        return h

    def _fetch_token(self) -> bool:
        if self.token:
            return True
        r = self.s.get(TOKEN_URL, company=SHARED_KEY, kind="text", robots=False, timeout=FEED_TIMEOUT)
        if not r.ok:
            self.errors.append(f"public token fetch failed: {r.error or r.status}")
            return False
        tok = (r.text or "").strip().splitlines()[-1].strip() if r.text else ""
        if not tok.startswith("eyJ"):
            self.errors.append("public token endpoint did not return a JWT")
            return False
        self.token = tok
        return True

    def _chain(self, start_epoch: float, stop_epoch: Optional[float]) -> int:
        """Walk the feed from ``start_epoch`` until the page reaches ``stop_epoch`` (or the feed end)."""
        url = FEED_URL
        headers = self._headers({"If-Modified-Since": email.utils.formatdate(start_epoch, usegmt=True)})
        pages = 0
        while url and pages < MAX_PAGES:
            r = self.s.get(url, company=SHARED_KEY, kind="json", robots=False, headers=headers, max_bytes=4_000_000,
                           timeout=FEED_TIMEOUT)
            headers = self._headers()
            if r.status == 304 or (r.ok and not r.body):
                break
            data = r.json() if r.ok else None
            if not isinstance(data, dict):
                self.errors.append(f"feed page failed: {r.error or r.status} at {url}")
                break
            pages += 1
            items = data.get("items") or []
            newest = None
            with self._lock:
                for it in items:
                    fe = it.get("_feed_entry") or {}
                    uuid = fe.get("uuid") or it.get("id")
                    if not uuid:
                        continue
                    when = fe.get("sistEndret") or it.get("date_modified") or ""
                    newest = max(newest or "", when)
                    prev = self.ads.get(uuid)
                    if prev is None or when >= prev["seen"]:
                        self.ads[uuid] = {"uuid": uuid, "status": fe.get("status"), "businessName": fe.get("businessName") or "",
                                          "municipal": fe.get("municipal"), "title": fe.get("title") or it.get("title"),
                                          "seen": when, "entry_url": BASE + (it.get("url") or f"/api/v1/feedentry/{uuid}"),
                                          "page_url": r.url, "page_sha256": r.sha256, "page_retrieved_at": r.retrieved_at,
                                          "page_snapshot": r.snapshot_path}
            nxt = data.get("next_url")
            url = (BASE + nxt) if nxt and nxt.startswith("/") else nxt
            if stop_epoch is not None and newest:
                try:
                    t = email.utils.parsedate_to_datetime(newest).timestamp() if "," in newest else _iso_epoch(newest)
                except Exception:
                    t = None
                if t is not None and t >= stop_epoch:
                    break
        return pages

    def build(self) -> bool:
        if not self._fetch_token():
            return False
        now = time.time()
        edges = [now - self.days * 86400 + i * (self.days * 86400 / self.chains) for i in range(self.chains)] + [None]
        with ThreadPoolExecutor(max_workers=self.chains) as pool:
            counts = list(pool.map(lambda i: self._chain(edges[i], edges[i + 1]), range(self.chains)))
        self.pages = sum(counts)
        for uuid, ad in self.ads.items():
            if ad.get("status") == "ACTIVE":
                self.by_name.setdefault(fold(ad["businessName"]), []).append(uuid)
        self.built = self.pages > 0
        self.built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return self.built

    # ---- lookup ------------------------------------------------------------------------------------------
    def candidates(self, legal_name: str, also: Optional[list] = None) -> list[dict]:
        """Active ads that could belong to this company, for the organisation-number gate to decide.

        A NAV employer name is usually the establishment, not the legal entity ("PARENT AS AVD OSLO"), so each
        name is matched as a substring. ``also`` carries the registry's own subunit names, because an
        establishment often trades under a name the parent's does not contain ("KVÆRNERBYEN FUS BARNEHAGE" under
        a differently named operator). A single generic token is never enough on its own: "C FRISØR AS" would
        otherwise pull in every hairdresser in Norway and waste the budget on certain rejections.
        """
        out: list[dict] = []
        seen: set[str] = set()
        for raw in [legal_name] + list(also or []):
            want = name_tokens(raw)
            if not want:
                continue
            target = fold(raw)
            stripped = " ".join(t for t in target.split() if t not in LEGAL_FORMS)
            for bname, uuids in self.by_name.items():
                if not bname:
                    continue
                hit = (bname == target or bname == stripped
                       or (len(stripped) >= 6 and stripped in bname)
                       or (len(stripped) >= 6 and bname in stripped)
                       or (len(want) >= 2 and set(want) <= set(bname.split())))
                if hit:
                    for u in uuids:
                        if u not in seen:
                            seen.add(u)
                            out.append(self.ads[u])
        out.sort(key=lambda a: a["seen"], reverse=True)
        return out[:MAX_CANDIDATES]

    def summary(self) -> dict:
        return {"pages": self.pages, "ads_seen": len(self.ads), "active_ads": sum(len(v) for v in self.by_name.values()),
                "days": self.days, "built_at": self.built_at, "token_source": "NAV_FEED_TOKEN" if os.environ.get("NAV_FEED_TOKEN") else "public",
                "errors": self.errors[:5]}


def _iso_epoch(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s).timestamp()


def fetch(profile: dict, session, index: FeedIndex, subunits: Optional[dict] = None) -> dict:
    """Publish only ads whose record carries this entity's organisation number, or that of one of its subunits.

    Norwegian job ads are posted by the establishment (underenhet), so ``employer.orgnr`` is usually the subunit
    number, not the parent's: NORDICNEUROLAB AS is org 891043082 and its ads carry 991095802, the subunit the
    official registry lists under it. Accepting a subunit number is therefore still exact-entity attribution, and
    the link is proved by the registry's own subunit endpoint rather than by name similarity. ``subunits`` maps
    subunit organisation number -> subunit name, taken from this run's official workplace claims.
    """
    ids = IdGen()
    org = str(profile.get("organisation_number") or "")
    name = profile.get("name") or ""
    subunits = {str(k): v for k, v in (subunits or {}).items()}
    claims: list[dict] = []
    evidence: list[dict] = []
    errors: list[dict] = []
    if not index.built:
        st = FAILED
        claims.append(new_claim(ids, "hiring", "active_job_count", None, st, [], prefix="job",
                                note="NAV feed scan failed this run: " + "; ".join(index.errors[:2])))
        errors.append(new_error("jobs_feed", "; ".join(index.errors[:2]) or "feed not built", st, FEED_URL))
        return {"claims": claims, "evidence": evidence, "errors": errors}
    cands = index.candidates(name, also=[v for v in subunits.values() if v])
    accepted = 0
    unchecked = 0
    rejected: list[str] = []
    for ad in cands:
        if session.remaining(org) < 2:
            errors.append(new_error("jobs_feed", "request budget exhausted before all NAV candidates were confirmed", AVAILABLE, ad["entry_url"]))
            unchecked += 1
            continue
        r = session.get(ad["entry_url"], company=org, kind="json", robots=False, headers=index._headers())
        data = r.json() if r.ok else None
        if not isinstance(data, dict):
            errors.append(new_error("jobs_feed", f"ad record fetch failed: {r.error or r.status}", BLOCKED if r.blocked else FAILED, ad["entry_url"]))
            unchecked += 1
            continue
        ac = data.get("ad_content") or {}
        emp = ac.get("employer") or {}
        ad_org = str(emp.get("orgnr") or "")
        via_subunit = ad_org in subunits and ad_org != org
        if ad_org != org and not via_subunit:
            rejected.append(f"{ad['businessName']} ({ad_org or 'no orgnr'})")
            continue
        if (data.get("status") or ad.get("status")) != "ACTIVE":
            rejected.append(f"{ad['businessName']} (inactive)")
            continue
        loc = (ac.get("workLocations") or [{}])[0] or {}
        value = {"title": ac.get("title") or ad.get("title"), "url": ac.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{ad['uuid']}",
                 "date_posted": (ac.get("published") or "")[:10] or None, "valid_through": (ac.get("expires") or "")[:10] or None,
                 "location": loc.get("city") or loc.get("municipal") or ad.get("municipal"), "source": "nav_feed",
                 "employer_name": emp.get("name"), "employer_orgnr": ad_org, "extent": ac.get("extent"),
                 "engagement_type": ac.get("engagementtype"), "positions": ac.get("positioncount"),
                 "posted_by_subunit": {"organisation_number": ad_org, "name": subunits.get(ad_org)} if via_subunit else None}
        span = json.dumps({"employer": {"name": emp.get("name"), "orgnr": ad_org}, "title": ac.get("title"),
                           "published": ac.get("published"), "expires": ac.get("expires")}, ensure_ascii=False)
        ev = new_evidence(ids, r, "official_job_board", span,
                          "nav_feed_entry_subunit_orgnr_match" if via_subunit else "nav_feed_entry_orgnr_match", prefix="evj")
        evidence.append(ev)
        claims.append(new_claim(ids, "hiring", "job_posting", value, AVAILABLE, [ev["id"]], prefix="job",
                                effective_date=value["date_posted"],
                                note=(f"posted by {subunits.get(ad_org) or 'a registered workplace'} (organisation number {ad_org}), "
                                      f"a subunit the official registry lists under this entity") if via_subunit else None))
        accepted += 1
    note = None
    count_state = AVAILABLE
    if unchecked and accepted == 0:
        count_state = FAILED
        note = f"{unchecked} name-matching NAV ad record(s) could not be fetched, so the count is unknown (not zero)"
    elif accepted == 0:
        note = f"scanned {index.summary()['active_ads']} active NAV ads from the last {index.days} days; none carried this organisation number"
        if rejected:
            note += "; name-similar ads rejected on org number: " + ", ".join(rejected[:5])
    elif unchecked:
        note = f"{accepted} confirmed; {unchecked} further name-matching record(s) could not be fetched, so the count is a floor"
    # evidence for the count: the feed scan itself (first page of the newest chain is representative)
    any_ad = next(iter(index.ads.values()), None)
    if count_state == FAILED:
        claims.append(new_claim(ids, "hiring", "active_job_count", None, FAILED, [], prefix="job", note=note))
    elif any_ad:
        fake = type("F", (), {})()
        fake.url = FEED_URL
        fake.final_url = any_ad["page_url"]
        fake.retrieved_at = any_ad["page_retrieved_at"]
        fake.status = 200
        fake.sha256 = any_ad["page_sha256"]
        fake.snapshot_path = any_ad["page_snapshot"]
        ev = new_evidence(ids, fake, "official_job_board", f"feed scan: {index.pages} pages, {index.summary()['active_ads']} active ads, {len(cands)} name candidates, {accepted} confirmed by orgnr",
                          "nav_feed_scan", prefix="evj")
        evidence.append(ev)
        claims.append(new_claim(ids, "hiring", "active_job_count", accepted, AVAILABLE, [ev["id"]], prefix="job", note=note))
    else:
        claims.append(new_claim(ids, "hiring", "active_job_count", accepted, AVAILABLE, [], prefix="job", note=note))
    return {"claims": claims, "evidence": evidence, "errors": errors}
