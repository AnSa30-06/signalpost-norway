"""NAV arbeidsplassen job connector: exact-legal-name gated job ads -> claims/evidence."""
from __future__ import annotations

import re
import urllib.parse

from .models import AVAILABLE, BLOCKED, FAILED, IdGen, new_claim, new_error, new_evidence

SEARCH_API = "https://arbeidsplassen.nav.no/stillinger/api/search"
AD_URL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
LEGAL_FORM_TOKENS = {"as", "asa", "ans", "da", "sa", "nuf", "ba", "enk", "ks", "se"}
STAGE = "jobs"


def fold(name) -> str:
    """casefold, æøå -> ae/o/a, punctuation -> space, collapse whitespace."""
    s = str(name or "").casefold().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def strip_legal_form(folded: str) -> str:
    toks = folded.split()
    if len(toks) > 1 and toks[-1] in LEGAL_FORM_TOKENS:
        toks = toks[:-1]
    return " ".join(toks)


def names_match(legal_name, candidate) -> bool:
    """Exact-name gate: folded equality, or legal name minus its legal-form token equals the candidate."""
    a, b = fold(legal_name), fold(candidate)
    return bool(a and b) and (a == b or strip_legal_form(a) == b)


def search_url(name: str) -> str:
    return SEARCH_API + "?" + urllib.parse.urlencode({"q": name, "size": 25})


def _date(v):
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def _location(src: dict):
    for loc in src.get("locationList") or []:
        if isinstance(loc, dict) and (loc.get("city") or loc.get("municipal")):
            return loc.get("city") or loc.get("municipal")
    return None


def fetch(profile: dict, session) -> dict:
    ids = IdGen()
    org = str(profile.get("organisation_number") or "")
    name = str(profile.get("name") or "").strip()
    claims, evidence, errors = [], [], []

    def count_claim(value, availability, note=None, ev=None):
        claims.append(new_claim(ids, "hiring", "active_job_count", value, availability, [ev["id"]] if ev else [],
                                confidence=0.95, note=note, prefix="job"))

    if not name:
        errors.append(new_error(STAGE, "profile has no legal name", FAILED))
        count_claim(None, FAILED, note="no legal name to search NAV with")
        return {"claims": claims, "evidence": evidence, "errors": errors}

    url = search_url(name)
    r = session.get(url, company=org, kind="json")
    data = r.json() if r.ok else None
    if not isinstance(data, dict):
        state = BLOCKED if r.blocked else FAILED
        msg = r.error or (f"http_{r.status}" if not r.ok else "response was not a JSON object")
        errors.append(new_error(STAGE, msg, state, source_url=url))
        count_claim(None, state, note=f"NAV job search failed: {msg}")
        return {"claims": claims, "evidence": evidence, "errors": errors}

    hits = data["hits"].get("hits") if isinstance(data.get("hits"), dict) else None
    hits = hits if isinstance(hits, list) else []
    accepted, seen = [], set()
    for hit in hits:
        src = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(src, dict) or src.get("status") != "ACTIVE":
            continue
        business = src.get("businessName")
        employer = (src.get("employer") or {}).get("name") if isinstance(src.get("employer"), dict) else None
        uuid = src.get("uuid")
        if not uuid or uuid in seen or not (names_match(name, business) or names_match(name, employer)):
            continue
        seen.add(uuid)
        accepted.append(src)
    accepted.sort(key=lambda s: str(s.get("published") or ""), reverse=True)

    for src in accepted:
        title = str(src.get("title") or "").strip() or None
        published = _date(src.get("published"))
        span = f"{src.get('businessName') or (src.get('employer') or {}).get('name')} — {title} — published {published}"
        ev = new_evidence(ids, r, "official_job_board", span, "nav_search_api_exact_name", prefix="evj")
        evidence.append(ev)
        value = {"title": title, "url": AD_URL.format(uuid=src["uuid"]), "date_posted": published,
                 "valid_through": _date(src.get("expires")), "location": _location(src), "source": "nav"}
        claims.append(new_claim(ids, "hiring", "job_posting", value, AVAILABLE, [ev["id"]], confidence=0.95,
                                effective_date=published, prefix="job"))

    span = f"{len(hits)} hits for {name!r}; {len(accepted)} active ads matched the exact legal name"
    ev = new_evidence(ids, r, "official_job_board", span, "nav_search_api_exact_name", prefix="evj")
    evidence.append(ev)
    note = None if accepted else "checked NAV job database; no active ads matched the exact legal name"
    count_claim(len(accepted), AVAILABLE, note=note, ev=ev)
    return {"claims": claims, "evidence": evidence, "errors": errors}
