"""Refresh regressions for the two faults named in Builderr's first evaluation report (docs/REMEDIATION.md, R7-R8).

A renamed company must produce ``changed_name``; a company that files accounts with no revenue line must still
produce ``new_filing`` when its reporting period advances; and diffing an envelope against itself must be empty.
"""
from __future__ import annotations

import copy

from signalpost import refresh
from signalpost.models import IdGen, new_claim


def _env(org, claims, run_id="r1", completed="2026-09-01T10:00:00Z"):
    return {"organisation_number": org, "schema_version": "1.0",
            "run": {"run_id": run_id, "started_at": completed, "completed_at": completed, "terminal_status": "completed",
                    "agent_version": "t", "refresh": {"baseline": False}},
            "identity": {}, "sections": {}, "claims": claims, "evidence": [], "changes": [], "errors": [],
            "operations": {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0, "bytes": 0, "budget_exhausted": False},
            "synthesis": {}}


def _holding_company(name, period_to, year, history, former=None):
    """A holding company: accounts filed, revenue line absent (not_available), period and history available."""
    ids = IdGen()
    claims = [
        new_claim(ids, "identity", "legal_name", name, "available", []),
        new_claim(ids, "identity", "legal_form", {"code": "AS", "label": "Aksjeselskap"}, "available", []),
        new_claim(ids, "identity", "business_address", {"street": "Storgata 1", "postcode": "0155", "city": "OSLO"}, "available", []),
        new_claim(ids, "identity", "status_flags", {"bankrupt": False, "liquidating": False, "forced_liquidation": False}, "available", []),
        new_claim(ids, "identity", "latest_submitted_accounts_year", year, "available", []),
        new_claim(ids, "accounts", "revenue", None, "not_available", [], note="field absent from the filed normalised accounts (not zero)",
                  reporting_period=f"{period_to[:4]}-01-01..{period_to}"),
        new_claim(ids, "accounts", "annual_result", 120000.0, "available", [], reporting_period=f"{period_to[:4]}-01-01..{period_to}"),
        new_claim(ids, "accounts", "reporting_period", {"from": f"{period_to[:4]}-01-01", "to": period_to}, "available", [],
                  reporting_period=f"{period_to[:4]}-01-01..{period_to}"),
        new_claim(ids, "accounts", "accounts_history", history, "available", []),
    ]
    if former:
        claims.append(new_claim(ids, "identity", "former_names", former, "available", []))
    return claims


def test_new_filing_is_detected_when_revenue_is_absent():
    prev = _env("910000001", _holding_company("EIENDOM HOLDING AS", "2024-12-31", "2024", ["2024-01-01..2024-12-31"]))
    cur = _env("910000001", _holding_company("EIENDOM HOLDING AS", "2025-12-31", "2025", ["2025-01-01..2025-12-31", "2024-01-01..2024-12-31"]),
               run_id="r2", completed="2026-09-12T10:00:00Z")
    changes = refresh.diff(prev, cur)
    filings = [c for c in changes if c["change_type"] == "new_filing"]
    assert len(filings) == 1, changes
    assert filings[0]["materiality"] == "material"
    # a missing revenue value must not surface as any other change type
    assert not any(c["field"] == "revenue" for c in changes)


def test_a_company_name_change_is_recorded():
    prev = _env("910000002", _holding_company("GAMMELT NAVN AS", "2025-12-31", "2025", ["2025-01-01..2025-12-31"]))
    cur = _env("910000002", _holding_company("NYTT NAVN AS", "2025-12-31", "2025", ["2025-01-01..2025-12-31"], former=["GAMMELT NAVN AS"]),
               run_id="r2", completed="2026-09-12T10:00:00Z")
    changes = refresh.diff(prev, cur)
    names = [c for c in changes if c["change_type"] == "changed_name"]
    assert len(names) == 1 and names[0]["previous_value"] == "GAMMELT NAVN AS" and names[0]["current_value"] == "NYTT NAVN AS"
    assert names[0]["materiality"] == "material"
    assert not any(c["change_type"] == "new_filing" for c in changes)


def test_address_and_status_changes_are_recorded():
    prev = _env("910000003", _holding_company("X AS", "2025-12-31", "2025", ["2025-01-01..2025-12-31"]))
    cur_claims = _holding_company("X AS", "2025-12-31", "2025", ["2025-01-01..2025-12-31"])
    for c in cur_claims:
        if c["field"] == "business_address":
            c["value"] = {"street": "Nygata 9", "postcode": "5003", "city": "BERGEN"}
        if c["field"] == "status_flags":
            c["value"] = {"bankrupt": False, "liquidating": True, "forced_liquidation": False}
    cur = _env("910000003", cur_claims, run_id="r2", completed="2026-09-12T10:00:00Z")
    types = {c["change_type"] for c in refresh.diff(prev, cur)}
    assert {"changed_address", "changed_status"} <= types


def test_diffing_an_envelope_against_itself_is_empty():
    env = _env("910000004", _holding_company("SAME AS", "2025-12-31", "2025", ["2025-01-01..2025-12-31"]))
    assert refresh.diff(env, copy.deepcopy(env)) == []


def test_one_new_filing_not_three():
    """Period, history and year all advance together; that is one filing, not three change records."""
    prev = _env("910000005", _holding_company("Y AS", "2024-12-31", "2024", ["2024-01-01..2024-12-31"]))
    cur = _env("910000005", _holding_company("Y AS", "2025-12-31", "2025", ["2025-01-01..2025-12-31", "2024-01-01..2024-12-31"]),
               run_id="r2", completed="2026-09-12T10:00:00Z")
    assert sum(1 for c in refresh.diff(prev, cur) if c["change_type"] == "new_filing") == 1
