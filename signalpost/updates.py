"""Brreg oppdateringer connector: dated registry update events -> registry_update claims."""
from __future__ import annotations

import json

from .models import AVAILABLE, BLOCKED, FAILED, NOT_AVAILABLE, IdGen, new_claim, new_error, new_evidence

UPDATES_API = "https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter"
MAX_EVENTS = 20
PAGE_SIZE = 200  # the API returns oldest-first and ignores sort=, so fetch one wide page and keep the newest
STAGE = "updates"


def updates_url(org: str) -> str:
    return f"{UPDATES_API}?organisasjonsnummer={org}&size={PAGE_SIZE}"


def fetch(profile: dict, session) -> dict:
    ids = IdGen()
    org = str(profile.get("organisation_number") or "")
    url = updates_url(org)
    r = session.get(url, company=org, kind="json")
    data = r.json() if r.ok else None
    if not isinstance(data, dict):
        state = BLOCKED if r.blocked else FAILED
        msg = r.error or (f"http_{r.status}" if not r.ok else "response was not a JSON object")
        claim = new_claim(ids, "activity", "registry_update", None, state, [], note=f"Brreg updates lookup failed: {msg}",
                          prefix="upd")
        return {"claims": [claim], "evidence": [], "errors": [new_error(STAGE, msg, state, source_url=url)]}

    embedded = data.get("_embedded") if isinstance(data.get("_embedded"), dict) else {}
    events = [e for e in (embedded.get("oppdaterteEnheter") or []) if isinstance(e, dict)]
    events.sort(key=lambda e: str(e.get("dato") or ""), reverse=True)
    events = events[:MAX_EVENTS]

    claims, evidence = [], []
    for e in events:
        dato = e.get("dato")
        date = dato[:10] if isinstance(dato, str) and len(dato) >= 10 else None
        span = json.dumps(e, ensure_ascii=False, separators=(",", ":"))[:300]
        ev = new_evidence(ids, r, "official_updates", span, "brreg_updates_api", prefix="evu")
        evidence.append(ev)
        value = {"date": date, "change_type": e.get("endringstype")}
        claims.append(new_claim(ids, "activity", "registry_update", value, AVAILABLE, [ev["id"]], effective_date=date,
                                prefix="upd"))
    if not claims:
        claims.append(new_claim(ids, "activity", "registry_update", None, NOT_AVAILABLE, [],
                                note="checked Brreg update feed; no update events recorded for this organisation number",
                                prefix="upd"))
    return {"claims": claims, "evidence": evidence, "errors": []}
