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
MATERIAL = {"new_filing", "changed_financials", "new_website", "changed_website", "new_role", "removed_role", "new_job",
            "closed_job", "new_location", "removed_location"}


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
        if f == "official_website":
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
                    if f == "revenue" or "revenue" not in c_state:
                        record(f, "new_filing", pc, cc, {"reporting_period": pp, "value": pc.get("value")},
                               {"reporting_period": cp, "value": cc.get("value")})
                elif pc.get("value") != cc.get("value"):
                    record(f, "changed_financials", pc, cc)
        else:
            for k in ck.keys() - pk.keys():
                if f in ADDED:
                    record(f, ADDED[f], None, ck[k])
            cur_sources = {(c.get("value") or {}).get("source") for c in ck.values() if isinstance(c.get("value"), dict)}
            if f == "job_posting" and c_state.get("active_job_count") == AVAILABLE:
                cur_sources.add("nav")
            for k in pk.keys() - ck.keys():
                if f in REMOVED:
                    src = (pk[k].get("value") or {}).get("source") if isinstance(pk[k].get("value"), dict) else None
                    if src and src not in cur_sources:
                        continue  # that source failed or was not read this run: keep the last supported value, no removal
                    record(f, REMOVED[f], pk[k], None)
        if ps is not None and ps != AVAILABLE and len(changes) == n_before:
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
