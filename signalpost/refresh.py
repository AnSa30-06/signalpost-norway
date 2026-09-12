"""Diff a previous envelope against the current one -> typed change records."""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .models import AVAILABLE

ADDED = {"job_posting": "new_job", "role": "new_role", "social_profile": "new_social_profile", "workplace": "new_location",
         "site_location": "new_location", "news_item": "new_news", "registry_update": "new_registry_update"}
REMOVED = {"job_posting": "closed_job", "role": "removed_role", "workplace": "removed_location",
           "site_location": "removed_location"}
ACCOUNT_FIELDS = ("revenue", "operating_result", "profit_before_tax", "annual_result", "total_assets", "equity",
                  "total_debt")
# Identity scalars, compared value-for-value (R7 in docs/REMEDIATION.md). A renamed company used to produce no
# change record at all, because only list-valued and financial fields were diffed.
IDENTITY_SCALARS = {"legal_name": "changed_name", "legal_form": "changed_legal_form", "business_address": "changed_address",
                    "registered_address": "changed_address", "status_flags": "changed_status",
                    "registry_website": "changed_registry_website", "registry_employees": "changed_employee_count",
                    "industry_code": "changed_industry", "public_brand": "changed_brand"}
# Filing-period fields, compared independently of every money field (R8): a company that files accounts with no
# revenue line still files, and 206 of 1,000 sampled companies do exactly that.
FILING_FIELDS = ("reporting_period", "accounts_history", "latest_submitted_accounts_year")
MATERIAL = {"new_filing", "changed_financials", "new_website", "changed_website", "new_role", "removed_role", "new_job",
            "closed_job", "new_location", "removed_location", "changed_name", "changed_legal_form", "changed_address",
            "changed_status", "changed_registry_website", "changed_industry"}


def _period_end(v) -> str:
    """'2025-01-01..2025-12-31' | {'from':..,'to':..} | '2025' -> the comparable end marker."""
    if isinstance(v, dict):
        return str(v.get("to") or v.get("tilDato") or "")
    if isinstance(v, str):
        return v.split("..")[-1]
    return str(v or "")


def _scalar(v):
    """Normalise a scalar or small dict for equality: whitespace/case-insensitive for strings."""
    if isinstance(v, str):
        return _fold(v)
    if isinstance(v, dict):
        return json.dumps({k: _scalar(x) for k, x in sorted(v.items())}, sort_keys=True, ensure_ascii=False)
    if isinstance(v, list):
        return json.dumps([_scalar(x) for x in v], ensure_ascii=False)
    return v


def norm_url(u) -> str:
    p = urllib.parse.urlsplit(str(u or "").strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def _fold(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold()


def claim_key(c: dict):
    field, v = c.get("field"), c.get("value")
    if isinstance(v, str) and v.startswith(("http://", "https://")):
        return field, norm_url(v)
    if isinstance(v, dict):
        if v.get("url"):
            return field, norm_url(v["url"])
        if v.get("name") and (v.get("role_code") or v.get("role")):
            return field, f"{_fold(v['name'])}|{_fold(v.get('role_code') or v.get('role'))}"
        if v.get("platform"):
            return field, _fold(v["platform"])
        if v.get("date") and v.get("change_type"):
            return field, f"{v['date']}|{v['change_type']}"
        if v.get("name"):
            return field, _fold(v["name"])
        return field, json.dumps(v, sort_keys=True, ensure_ascii=False)
    return field, ""


def _ev_times(env: dict) -> dict:
    return {e.get("id"): e.get("retrieved_at") for e in env.get("evidence") or [] if isinstance(e, dict)}


def _observed(claim: dict, env: dict, times: dict) -> str | None:
    ts = [times[i] for i in claim.get("evidence_ids") or [] if times.get(i)]
    return min(ts) if ts else (env.get("run") or {}).get("started_at")


def _index(env: dict):
    """-> (available claims by key, field -> availability state, field -> first claim)."""
    by_key, state, first = {}, {}, {}
    for c in env.get("claims") or []:
        if not isinstance(c, dict) or not c.get("field"):
            continue
        f = c["field"]
        first.setdefault(f, c)
        if c.get("availability") == AVAILABLE:
            state[f] = AVAILABLE
            by_key.setdefault(claim_key(c), c)
        else:
            state.setdefault(f, c.get("availability"))
    return by_key, state, first


def diff(previous: dict | None, current: dict) -> list[dict]:
    if not previous:
        return []
    now = (current.get("run") or {}).get("completed_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p_keys, p_state, p_first = _index(previous)
    c_keys, c_state, c_first = _index(current)
    p_times, c_times = _ev_times(previous), _ev_times(current)
    changes: list[dict] = []

    def record(field, ctype, prev_claim, cur_claim, prev_value=None, cur_value=None, section=None):
        first = _observed(prev_claim, previous, p_times) if prev_claim else None
        first = first or (_observed(cur_claim, current, c_times) if cur_claim else None) or now
        material = ctype in MATERIAL or (ctype == "availability_changed" and section in ("web", "accounts"))
        changes.append({"field": field, "change_type": ctype, "materiality": "material" if material else "minor",
                        "previous_value": prev_claim.get("value") if prev_claim and prev_value is None else prev_value,
                        "current_value": cur_claim.get("value") if cur_claim and cur_value is None else cur_value,
                        "first_observed": first, "last_observed": now,
                        "evidence_ids": list((cur_claim or {}).get("evidence_ids") or []),
                        "previous_evidence_ids": list((prev_claim or {}).get("evidence_ids") or [])})

    filing_recorded = False

    def _avail(env_keys, f):
        return next((c for k, c in env_keys.items() if k[0] == f), None)

    # R8: a new filing is detected from the period itself, before any money field is looked at, so that a
    # company whose revenue line is absent still gets its new filing recorded.
    for f in FILING_FIELDS:
        pc, cc = _avail(p_keys, f), _avail(c_keys, f)
        if not pc or not cc or filing_recorded:
            continue
        if f == "accounts_history":
            new_periods = [x for x in (cc.get("value") or []) if x not in (pc.get("value") or [])]
            if new_periods and max(_period_end(x) for x in new_periods) > max((_period_end(x) for x in (pc.get("value") or [])), default=""):
                record(f, "new_filing", pc, cc, pc.get("value"), {"new_periods": new_periods})
                filing_recorded = True
        elif _period_end(cc.get("value")) > _period_end(pc.get("value")):
            record(f, "new_filing", pc, cc)
            filing_recorded = True

    fields = sorted(set(p_state) | set(c_state))
    for f in fields:
        ps, cs = p_state.get(f), c_state.get(f)
        n_before = len(changes)
        if ps == AVAILABLE and cs is not None and cs != AVAILABLE:  # lost availability: no per-key removals
            record(f, "availability_changed", p_first.get(f), c_first.get(f), ps, cs, section=c_first[f].get("section"))
            continue
        if cs != AVAILABLE:
            continue
        pk = {k: c for k, c in p_keys.items() if k[0] == f}
        ck = {k: c for k, c in c_keys.items() if k[0] == f}
        if f in FILING_FIELDS:
            continue  # handled above
        if f in IDENTITY_SCALARS:
            pc, cc = next(iter(pk.values()), None), next(iter(ck.values()), None)
            if pc and cc and _scalar(pc.get("value")) != _scalar(cc.get("value")):
                record(f, IDENTITY_SCALARS[f], pc, cc)
        elif f == "former_names":
            # only when legal_name itself did not change this run (that branch records the rename with both names)
            pl, cl = _avail(p_keys, "legal_name"), _avail(c_keys, "legal_name")
            if pl and cl and _scalar(pl.get("value")) != _scalar(cl.get("value")):
                continue
            pc, cc = next(iter(pk.values()), None), next(iter(ck.values()), None)
            added = [x for x in (cc.get("value") or []) if x not in ((pc or {}).get("value") or [])] if cc else []
            if added and not any(ch["change_type"] == "changed_name" for ch in changes):
                record(f, "changed_name", pc, cc, (pc or {}).get("value"), {"former_names_added": added})
        elif f == "official_website":
            pc, cc = next(iter(pk.values()), None), next(iter(ck.values()), None)
            if cc and not pc:
                record(f, "new_website", None, cc)
            elif pc and cc and claim_key(pc) != claim_key(cc):
                record(f, "changed_website", pc, cc)
        elif f == "website_description":
            pc, cc = next(iter(pk.values()), None), next(iter(ck.values()), None)
            if pc and cc and pc.get("value") != cc.get("value"):
                record(f, "changed_description", pc, cc)
        elif f in ACCOUNT_FIELDS:
            pc, cc = next(iter(pk.values()), None), next(iter(ck.values()), None)
            if pc and cc:
                pp, cp = pc.get("reporting_period"), cc.get("reporting_period")
                if pp != cp:
                    if not filing_recorded:
                        record(f, "new_filing", pc, cc, {"reporting_period": pp, "value": pc.get("value")},
                               {"reporting_period": cp, "value": cc.get("value")})
                        filing_recorded = True
                elif pc.get("value") != cc.get("value"):
                    record(f, "changed_financials", pc, cc)
        else:
            for k in ck.keys() - pk.keys():
                if f in ADDED:
                    record(f, ADDED[f], None, ck[k])
            def _family(src):
                return "nav" if src in ("nav", "nav_feed") else src  # both NAV routes are one source family
            cur_sources = {_family((c.get("value") or {}).get("source")) for c in ck.values() if isinstance(c.get("value"), dict)}
            if f == "job_posting" and c_state.get("active_job_count") == AVAILABLE:
                cur_sources.add("nav")
            for k in pk.keys() - ck.keys():
                if f in REMOVED:
                    src = (pk[k].get("value") or {}).get("source") if isinstance(pk[k].get("value"), dict) else None
                    if src and _family(src) not in cur_sources:
                        continue  # that source failed or was not read this run: keep the last supported value, no removal
                    record(f, REMOVED[f], pk[k], None)
        if ps is not None and ps != AVAILABLE and len(changes) == n_before and f not in FILING_FIELDS:
            record(f, "availability_changed", p_first.get(f), c_first.get(f), ps, cs, section=c_first[f].get("section"))
    return changes


def load_previous(path) -> dict:
    """JSONL of envelopes -> {organisation_number: envelope}; the last line per org wins; {} if missing."""
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict = {}
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                env = json.loads(line)
            except ValueError:
                continue
            if isinstance(env, dict) and env.get("organisation_number"):
                out[str(env["organisation_number"])] = env
    return out
