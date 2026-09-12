"""Per-company orchestration: official anchor -> dated updates -> jobs -> website discovery + crawl -> diff -> synthesis.

Exactly one envelope is produced for every input, whatever fails. Request priority is fixed so that the
cheap official sources always run before the request-hungry website crawl.
"""
from __future__ import annotations

import subprocess
import time
import traceback
from typing import Optional

from . import __version__
from .models import (AMBIGUOUS, AVAILABLE, BLOCKED, FAILED, NOT_AVAILABLE, STATES, IdGen, new_claim, new_error,
                     new_evidence, state_from_fetch, validate_envelope)
from .net import Session, utc_now
from .registry import Official

SCHEMA_VERSION = "1.0"
SECTION_ORDER = ("identity", "accounts", "leadership", "workplaces", "web", "hiring", "activity")
MAX_SITE_CANDIDATES = 4   # registry site, registry e-mail domain, and two name-derived guesses
SITE_PAGE_BUDGET = 7


def agent_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
        return f"{__version__}+{sha}" if sha else __version__
    except Exception:
        return __version__


def _merge(target_claims, target_ev, target_err, result: dict) -> None:
    target_claims.extend(result.get("claims") or [])
    target_ev.extend(result.get("evidence") or [])
    target_err.extend(result.get("errors") or [])


def _section_state(claims: list[dict], section: str, default: str) -> str:
    states = [c["availability"] for c in claims if c["section"] == section]
    if not states:
        return default
    if AVAILABLE in states:
        return AVAILABLE
    for st in (AMBIGUOUS, BLOCKED, NOT_AVAILABLE, FAILED):
        if st in states:
            return st
    return states[0]


def process_company(org: str, row: Optional[dict], session: Session, run_id: str, previous: Optional[dict],
                    started_run_at: str, nav_index=None) -> dict:
    from . import discovery, identity, jobs, navfeed, refresh, site, synthesis, updates  # local import: modules built separately

    t0 = time.monotonic()
    started = utc_now()
    ids = IdGen()
    claims: list[dict] = []
    evidence: list[dict] = []
    errors: list[dict] = []
    sections: dict[str, str] = {}
    profile = dict(row or {"organisation_number": org, "name": None, "legal_form": None, "municipality": None})
    profile["organisation_number"] = org

    # 1. official anchor ------------------------------------------------------------------------------------
    off = Official(session, ids, org, row)
    for step in (off.identity, off.accounts, off.filing_years, off.roles, off.workplaces):
        try:
            step()
        except Exception as exc:  # never lose a company to one module
            errors.append(new_error(step.__name__, f"{type(exc).__name__}: {exc}", FAILED))
    claims.extend(off.claims)
    evidence.extend(off.evidence)
    errors.extend(off.errors)
    sections.update(off.sections)
    profile["registry"] = off.entity
    if off.entity:
        profile["name"] = off.entity.get("navn") or profile.get("name")
        profile["legal_form"] = ((off.entity.get("organisasjonsform") or {}).get("kode")) or profile.get("legal_form")
        ba = off.entity.get("forretningsadresse") or off.entity.get("postadresse") or {}
        profile["municipality"] = ba.get("kommune") or profile.get("municipality")
        if off.entity.get("hjemmeside"):
            profile["website"] = off.entity["hjemmeside"]
    legal_name = profile.get("name")

    # 2. dated official updates (1 request) ---------------------------------------------------------------
    try:
        _merge(claims, evidence, errors, updates.fetch(profile, session))
    except Exception as exc:
        errors.append(new_error("updates", f"{type(exc).__name__}: {exc}", FAILED))

    # 3. NAV jobs (1 request) --------------------------------------------------------------------------------
    if legal_name:
        try:
            if nav_index is not None and nav_index.built:
                # registered workplaces from this run's official subunit claims: a NAV ad usually carries the
                # subunit's organisation number, and the registry link is what makes accepting it exact.
                subunits = {str(c["value"]["organisation_number"]): c["value"].get("name")
                            for c in claims if c["field"] == "workplace" and c["availability"] == AVAILABLE
                            and isinstance(c.get("value"), dict) and c["value"].get("organisation_number")}
                _merge(claims, evidence, errors, navfeed.fetch(profile, session, nav_index, subunits=subunits))
            else:
                _merge(claims, evidence, errors, jobs.fetch(profile, session))                 # search-API fallback, exact-name gate
        except Exception as exc:
            errors.append(new_error("jobs", f"{type(exc).__name__}: {exc}", FAILED))
            claims.append(new_claim(ids, "hiring", "active_job_count", None, FAILED, [], note=f"job connector crashed: {exc}"))
    else:
        claims.append(new_claim(ids, "hiring", "active_job_count", None, NOT_AVAILABLE, [], note="no legal name available to query the job database"))

    # 4. website discovery + identity gate + crawl ------------------------------------------------------------
    web_state = NOT_AVAILABLE
    web_note = "no website candidate passed the exact-entity gate"
    best_review = None
    probed: list[dict] = []
    identity_result = None
    try:
        cands = discovery.candidates(profile, session) if legal_name else []
    except Exception as exc:
        cands = []
        errors.append(new_error("discovery", f"{type(exc).__name__}: {exc}", FAILED))
    def probe_candidates(cand_list):
        """Fetch each candidate and gate it. Returns True as soon as one proves the entity."""
        nonlocal web_state, best_review, identity_result
        for cand in cand_list[:MAX_SITE_CANDIDATES]:
            if session.remaining(org) < 3:
                errors.append(new_error("website", "request budget exhausted before all candidates were probed", NOT_AVAILABLE, cand["url"]))
                return False
            page = session.get(cand["url"], company=org, kind="html")
            if not page.ok and page.error and "tls" in page.error and cand["url"].startswith("https://") and session.remaining(org) > 3:
                page = session.get("http://" + cand["url"][len("https://"):], company=org, kind="html")  # broken TLS: plain-http fallback, still identity-gated
            entry = {"url": cand["url"], "origin": cand["origin"], "status": page.status, "error": page.error}
            if not page.ok or ("html" not in (page.content_type or "").lower() and "<html" not in page.text[:2000].lower()):
                err = page.error or ""
                if page.blocked:
                    entry["verdict"] = "blocked"
                elif "dns failure" in err or page.status in (404, 410) or "malformed host" in err:
                    entry["verdict"] = "no_site"          # the guessed domain does not exist or serves nothing: not a failure
                else:
                    entry["verdict"] = "unreachable"      # resolved but timed out / reset / broken TLS
                probed.append(entry)
                continue
            try:
                ident = identity.assess(profile, page)
            except Exception as exc:
                errors.append(new_error("identity", f"{type(exc).__name__}: {exc}", FAILED, cand["url"]))
                probed.append({**entry, "verdict": "identity_error"})
                continue
            entry.update({"verdict": ident.get("status"), "score": ident.get("score"), "reasons": ident.get("reasons")})
            probed.append(entry)
            if ident.get("status") == "exact":
                identity_result = ident
                try:
                    _merge(claims, evidence, errors, site.crawl(profile, session, page, ident, page_budget=SITE_PAGE_BUDGET))
                except Exception as exc:
                    errors.append(new_error("site", f"{type(exc).__name__}: {exc}", FAILED, cand["url"]))
                    ev = new_evidence(ids, page, "company_owned", ident.get("claim_span") or "", "identity_gate")
                    evidence.append(ev)
                    claims.append(new_claim(ids, "web", "official_website", page.final_url, AVAILABLE, [ev["id"]], ident.get("score", 0.9),
                                            note="site verified; deeper crawl failed"))
                web_state = AVAILABLE
                return True
            if ident.get("status") == "review" and best_review is None and len(identity.name_tokens(legal_name)) >= 2:
                best_review = (cand, page, ident)
        return False

    if not probe_candidates(cands) and legal_name and discovery.brave_enabled() and session.remaining(org) >= 4:
        # Only now is a paid search query worth spending: the registry field and the domain guesses have failed.
        try:
            extra = discovery.brave_candidates(profile, session)
            known = {p["url"] for p in probed}
            probe_candidates([c for c in extra if c["url"] not in known])
        except Exception as exc:
            errors.append(new_error("discovery_search", f"{type(exc).__name__}: {exc}", FAILED))

    if web_state != AVAILABLE:
        if best_review is not None:
            cand, page, ident = best_review
            ev = new_evidence(ids, page, "company_owned", ident.get("claim_span") or (page.text[:200]), "identity_gate")
            evidence.append(ev)
            claims.append(new_claim(ids, "web", "official_website", cand["url"], AMBIGUOUS, [ev["id"]], ident.get("score", 0.8),
                                    note=f"candidate matched the company name ({', '.join(ident.get('reasons') or [])}) but neither the organisation "
                                         f"number nor the registered address was found on the page; not published as verified"))
            web_state = AMBIGUOUS
        else:
            registry_probe = next((p for p in probed if p["origin"] == "registry"), None)
            tried = "; ".join(f"{p['url']} ({p.get('verdict')}{': ' + str(p.get('error') or p.get('status')) if p.get('verdict') != 'no_site' else ''})" for p in probed)
            if not cands:
                web_note = "no website candidates: the registry lists no site and the legal name yields no distinctive domain guess" if legal_name else "no legal name"
            elif registry_probe and registry_probe.get("verdict") == "blocked":
                web_state, web_note = BLOCKED, f"the registry-listed website refused the crawler ({registry_probe.get('error')}); tried: {tried}"
            elif registry_probe and registry_probe.get("verdict") == "unreachable":
                web_state, web_note = FAILED, f"the registry-listed website could not be fetched ({registry_probe.get('error')}); tried: {tried}"
            elif registry_probe and registry_probe.get("verdict") in ("related_or_uncertain", "review"):
                web_state, web_note = AMBIGUOUS, f"the registry-listed website did not prove this exact entity ({registry_probe.get('reasons')}); tried: {tried}"
            elif probed and all(p.get("verdict") == "unreachable" for p in probed):
                # every candidate resolved but could not be read: we do not know whether a site exists.
                web_state, web_note = FAILED, f"every website candidate resolved but could not be fetched; tried: {tried}"
            else:
                web_note = f"no website found: registry lists none and no domain guess proved the exact entity; tried: {tried}"
            claims.append(new_claim(ids, "web", "official_website", None, web_state, [], note=web_note[:600]))

    # 5. sections ----------------------------------------------------------------------------------------------
    sections.setdefault("identity", _section_state(claims, "identity", FAILED))
    sections.setdefault("accounts", _section_state(claims, "accounts", FAILED))
    sections.setdefault("leadership", _section_state(claims, "leadership", FAILED))
    sections.setdefault("workplaces", _section_state(claims, "workplaces", FAILED))
    sections["web"] = web_state
    sections["hiring"] = _section_state(claims, "hiring", FAILED)
    sections["activity"] = _section_state(claims, "activity", NOT_AVAILABLE)
    sections = {k: sections[k] for k in SECTION_ORDER}

    # 6. envelope, diff, synthesis --------------------------------------------------------------------------------
    completed = utc_now()
    official_site = next((c["value"] for c in claims if c["field"] == "official_website" and c["availability"] == AVAILABLE), None)
    brand = next((c["value"] for c in claims if c["field"] == "public_brand" and c["availability"] == AVAILABLE), None)
    form = next((c["value"] for c in claims if c["field"] == "legal_form" and c["availability"] == AVAILABLE), None)
    envelope = {
        "organisation_number": org,
        "schema_version": SCHEMA_VERSION,
        "run": {"run_id": run_id, "started_at": started, "completed_at": completed, "terminal_status": "completed",
                "agent_version": agent_version(), "previous_run_id": (previous or {}).get("run", {}).get("run_id"),
                "refresh": {"baseline": previous is None, "previous_snapshot": (previous or {}).get("run", {}).get("completed_at")}},
        "identity": {"legal_name": legal_name, "legal_form": (form or {}).get("code") if isinstance(form, dict) else profile.get("legal_form"),
                     "municipality": profile.get("municipality"), "industry_code": next((c["value"] for c in claims if c["field"] == "industry_code"), None),
                     "industry_label": next((c["value"] for c in claims if c["field"] == "industry_label"), None),
                     "public_brand": brand, "official_website": official_site,
                     "website_identity": {k: identity_result.get(k) for k in ("score", "reasons")} if identity_result else None},
        "sections": sections,
        "claims": claims,
        "evidence": evidence,
        "changes": [],
        "errors": errors,
        "operations": {"requests": session.requests_used(org), "runtime_ms": int((time.monotonic() - t0) * 1000),
                       "third_party_cost_usd": 0.0, "bytes": sum(r.get("bytes", 0) for r in session.log if r.get("company") == org),
                       "budget_exhausted": session.remaining(org) <= 0 or any("budget" in (e.get("message") or "") for e in errors)},
        "synthesis": {},
        "diagnostics": {"website_candidates_probed": probed},
    }
    try:
        envelope["changes"] = refresh.diff(previous, envelope)
    except Exception as exc:
        errors.append(new_error("refresh", f"{type(exc).__name__}: {exc}", FAILED))
    try:
        envelope["synthesis"] = synthesis.build(envelope)
    except Exception as exc:
        errors.append(new_error("synthesis", f"{type(exc).__name__}: {exc}", FAILED))
        envelope["synthesis"] = {"summary": f"{legal_name or org}: synthesis unavailable ({exc})", "cannot_establish": []}
    failed_sections = [k for k, v in sections.items() if v == FAILED]
    if sections["identity"] == FAILED:
        envelope["run"]["terminal_status"] = "failed"
    elif failed_sections:
        envelope["run"]["terminal_status"] = "completed_with_errors"
    problems = validate_envelope(envelope)
    if problems:
        envelope["errors"].append(new_error("validation", "; ".join(problems)[:300], FAILED))
    return envelope


def failed_envelope(org: str, run_id: str, started: str, message: str) -> dict:
    """Terminal envelope for a company whose processing crashed outright."""
    return {"organisation_number": org, "schema_version": SCHEMA_VERSION,
            "run": {"run_id": run_id, "started_at": started, "completed_at": utc_now(), "terminal_status": "failed",
                    "agent_version": agent_version(), "previous_run_id": None, "refresh": {"baseline": True, "previous_snapshot": None}},
            "identity": {"legal_name": None}, "sections": {k: FAILED for k in SECTION_ORDER}, "claims": [], "evidence": [], "changes": [],
            "errors": [new_error("pipeline", message, FAILED)],
            "operations": {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0, "bytes": 0, "budget_exhausted": False},
            "synthesis": {"summary": f"Processing failed for {org}: {message[:120]}", "cannot_establish": ["Everything: the pipeline crashed before any source was read."]}}
