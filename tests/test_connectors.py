"""Offline tests for jobs, updates, synthesis and refresh (agent B modules)."""
from __future__ import annotations

import copy
import hashlib
import json

from signalpost import jobs, refresh, synthesis, updates
from signalpost.net import FetchResult

PROFILE = {"organisation_number": "910209205", "name": "AF GRUPPEN ASA", "legal_form": "ASA", "municipality": "OSLO"}
RETRIEVED = "2026-09-09T10:00:00Z"


class FakeSession:
    """Returns the same canned FetchResult for every URL; records what was requested."""

    def __init__(self, body: str = "", status: int | None = 200, error: str | None = None, blocked: bool = False):
        self.body, self.status, self.error, self.blocked = body, status, error, blocked
        self.calls: list[str] = []

    def get(self, url, company, kind="html", **kw):
        self.calls.append(url)
        raw = self.body.encode("utf-8")
        return FetchResult(url=url, final_url=url, status=self.status, body=raw, text=self.body,
                           sha256=hashlib.sha256(raw).hexdigest() if raw else "", retrieved_at=RETRIEVED,
                           snapshot_path="snapshots/fake.json" if raw else "", error=self.error, blocked=self.blocked)


def nav_hit(uuid, business, employer=None, status="ACTIVE", title="Anleggsleder", published="2026-08-20T08:00:00Z"):
    return {"_source": {"uuid": uuid, "title": title, "businessName": business, "employer": {"name": employer or business},
                        "published": published, "expires": "2026-10-01T00:00:00Z", "status": status, "source": "FINN",
                        "locationList": [{"city": "OSLO", "municipal": "OSLO"}], "properties": {}}}


def nav_body(*hits):
    return json.dumps({"hits": {"total": {"value": len(hits)}, "hits": list(hits)}})


# --- jobs ---------------------------------------------------------------------------------------------------
def test_nav_exact_name_gate_accepts_and_rejects():
    body = nav_body(nav_hit("a1", "AF Gruppen ASA"),                          # exact (case-insensitive)
                    nav_hit("a2", "AF GRUPPEN NORGE AS AF ANLEGG"),           # sister company -> reject
                    nav_hit("a3", "AF Gruppen ASA", status="INACTIVE"),       # not active -> skip
                    nav_hit("a4", "AF Anlegg", employer="AF GRUPPEN ASA"),    # employer.name matches
                    nav_hit("a5", "AF Gruppen", published="2026-09-01T00:00:00Z"))  # name minus legal form
    s = FakeSession(body)
    out = jobs.fetch(PROFILE, s)
    assert s.calls == ["https://arbeidsplassen.nav.no/stillinger/api/search?q=AF+GRUPPEN+ASA&size=25"]
    postings = [c for c in out["claims"] if c["field"] == "job_posting"]
    urls = {c["value"]["url"] for c in postings}
    assert urls == {f"https://arbeidsplassen.nav.no/stillinger/stilling/{u}" for u in ("a1", "a4", "a5")}
    assert postings[0]["value"]["url"].endswith("/a5")  # newest first
    assert postings[0]["value"] == {"title": "Anleggsleder", "url": "https://arbeidsplassen.nav.no/stillinger/stilling/a5",
                                    "date_posted": "2026-09-01", "valid_through": "2026-10-01", "location": "OSLO",
                                    "source": "nav"}
    assert all(c["id"].startswith("job-") and c["section"] == "hiring" for c in out["claims"])
    ev = {e["id"]: e for e in out["evidence"]}
    assert all(e.startswith("evj-") for e in ev)
    span = ev[postings[0]["evidence_ids"][0]]["claim_span"]
    assert span == "AF Gruppen — Anleggsleder — published 2026-09-01"
    assert ev[postings[0]["evidence_ids"][0]]["extraction_method"] == "nav_search_api_exact_name"
    count = next(c for c in out["claims"] if c["field"] == "active_job_count")
    assert count["value"] == 3 and count["availability"] == "available" and count["evidence_ids"]
    assert out["errors"] == []


def test_names_match_rules():
    assert jobs.names_match("AF GRUPPEN ASA", "af gruppen asa")
    assert jobs.names_match("AF GRUPPEN ASA", "AF Gruppen")
    assert jobs.names_match("Bjørn Bygg AS", "BJORN BYGG AS")
    assert not jobs.names_match("AF GRUPPEN ASA", "AF GRUPPEN NORGE AS")
    assert not jobs.names_match("AF GRUPPEN ASA", "AF GRUPPEN NORGE AS AF ANLEGG")
    assert not jobs.names_match("AF GRUPPEN ASA", None)


def test_nav_zero_match_gives_zero_count_available():
    out = jobs.fetch(PROFILE, FakeSession(nav_body(nav_hit("x1", "AF GRUPPEN NORGE AS"))))
    assert [c["field"] for c in out["claims"]] == ["active_job_count"]
    c = out["claims"][0]
    assert c["value"] == 0 and c["availability"] == "available" and "no active ads matched" in c["note"]
    assert c["evidence_ids"] and out["errors"] == []


def test_nav_fetch_failure_gives_failed_or_blocked():
    out = jobs.fetch(PROFILE, FakeSession("", status=None, error="network: timed out"))
    c = out["claims"][0]
    assert c["field"] == "active_job_count" and c["availability"] == "failed" and c["value"] is None
    assert out["errors"] and out["errors"][0]["availability"] == "failed"
    out = jobs.fetch(PROFILE, FakeSession("", status=403, error="http_403", blocked=True))
    assert out["claims"][0]["availability"] == "blocked"
    out = jobs.fetch(PROFILE, FakeSession("<html>not json</html>"))
    assert out["claims"][0]["availability"] == "failed"


# --- updates ------------------------------------------------------------------------------------------------
def test_updates_parse_newest_first():
    body = json.dumps({"_embedded": {"oppdaterteEnheter": [
        {"oppdateringsid": 1, "dato": "2018-06-02T14:20:08.703Z", "organisasjonsnummer": "910209205", "endringstype": "Ukjent"},
        {"oppdateringsid": 2, "dato": "2024-01-15T09:00:00.000Z", "organisasjonsnummer": "910209205", "endringstype": "Endring"}]}})
    out = updates.fetch(PROFILE, FakeSession(body))
    assert [c["value"] for c in out["claims"]] == [{"date": "2024-01-15", "change_type": "Endring"},
                                                   {"date": "2018-06-02", "change_type": "Ukjent"}]
    c = out["claims"][0]
    assert c["section"] == "activity" and c["field"] == "registry_update" and c["effective_date"] == "2024-01-15"
    assert c["id"].startswith("upd-") and c["availability"] == "available"
    ev = out["evidence"][0]
    assert ev["id"] == c["evidence_ids"][0] and ev["id"].startswith("evu-")
    assert ev["extraction_method"] == "brreg_updates_api" and '"endringstype":"Endring"' in ev["claim_span"]
    assert ev["source_class"] == "official_updates"


def test_updates_empty_and_failed():
    out = updates.fetch(PROFILE, FakeSession(json.dumps({"page": {"totalElements": 0}})))
    assert len(out["claims"]) == 1 and out["claims"][0]["availability"] == "not_available" and out["claims"][0]["note"]
    out = updates.fetch(PROFILE, FakeSession("", status=500, error="http_500"))
    assert out["claims"][0]["availability"] == "failed" and out["errors"]


# --- synthesis ----------------------------------------------------------------------------------------------
def claim(field, value, section="identity", availability="available", note=None, period=None, cid=None, ev=None):
    return {"id": cid or f"c-{field}", "section": section, "field": field, "value": value, "availability": availability,
            "confidence": 1.0, "evidence_ids": ev if ev is not None else (["ev-1"] if availability == "available" else []),
            "reporting_period": period, "effective_date": None, "note": note}


def envelope(claims, changes=None, baseline=True, evidence=None):
    return {"organisation_number": "912345678", "schema_version": "1.0",
            "run": {"run_id": "r1", "started_at": RETRIEVED, "completed_at": "2026-09-09T10:05:00Z",
                    "terminal_status": "completed", "agent_version": "test", "previous_run_id": None,
                    "refresh": {"baseline": baseline, "previous_snapshot": None}},
            "identity": {"legal_name": "Sandnes Elektriske AS"}, "sections": {}, "claims": claims,
            "evidence": evidence or [{"id": "ev-1", "source_url": "https://data.brreg.no/x", "final_url": None,
                                      "source_class": "official_registry", "retrieved_at": RETRIEVED, "http_status": 200,
                                      "content_sha256": None, "snapshot_path": None, "claim_span": "x",
                                      "extraction_method": "t", "reporting_period": None}],
            "changes": changes or [], "errors": [], "operations": {}, "synthesis": {}}


def test_synthesis_summary_and_cannot_establish():
    env = envelope([
        claim("legal_name", "Sandnes Elektriske AS"), claim("legal_form", "AS"), claim("municipality", "SANDNES"),
        claim("industry_code", "43.210"), claim("industry_label", "Elektrisk installasjonsarbeid"),
        claim("registry_employees", 11),
        claim("revenue", 24_100_000, section="accounts", period="2025"),
        claim("annual_result", -1_200_000, section="accounts", period="2025"),
        claim("role", {"name": "Ola Nordmann", "role": "Daglig leder", "role_code": "DAGL", "since": None}, section="leadership"),
        claim("role", {"name": "Kari Nordmann", "role": "Styrets leder", "role_code": "LEDE", "since": None}, section="leadership", cid="c-role2"),
        claim("official_website", None, section="web", availability="not_available",
              note="candidate found but the organisation number and address were not confirmed on the page"),
        claim("active_job_count", 2, section="hiring"),
        claim("job_posting", {"title": "Elektriker", "url": "https://arbeidsplassen.nav.no/stillinger/stilling/j1",
                              "date_posted": "2026-08-01", "valid_through": None, "location": "SANDNES", "source": "nav"}, section="hiring"),
        claim("news_item", {"title": "Nytt kontor", "url": "https://x.no/n", "date": "2026-08-20"}, section="activity"),
        claim("registry_update", None, section="activity", availability="failed", note="Brreg updates lookup failed: http_500"),
    ])
    out = synthesis.build(env)
    s = out["summary"]
    assert s.startswith("Sandnes Elektriske AS is a Norwegian private limited company (AS) in Sandnes registered under industry 43.210 Elektrisk installasjonsarbeid.")
    assert "The registry reports 11 employees." in s
    assert "For the 2025 accounting year it filed revenue of NOK 24.1 million and a loss of NOK 1.2 million." in s
    assert "The general manager is Ola Nordmann; the board chair is Kari Nordmann." in s
    assert "It has 2 active job ads on NAV." in s
    assert "Latest dated activity: news item “Nytt kontor” on 2026-08-20." in s
    assert "sandneselektriske" not in s  # the not_available website must never be stated
    assert out["what_changed"] == "First run — no previous snapshot."
    assert any(e.startswith("No official website could be found (candidate found but") for e in out["cannot_establish"])
    assert any("registry update history could be retrieved" in e for e in out["cannot_establish"])
    assert set(out.keys()) == {"summary", "what_it_does", "size", "trend", "risk_flags", "leadership", "footprint", "hiring", "recent_activity", "what_changed", "verification", "answers", "cannot_establish"}
    assert "Elektriker" in out["hiring"]


def test_synthesis_money_and_defensiveness():
    assert synthesis.money(1_234_000_000) == "NOK 1.23 billion"
    assert synthesis.money(24_000_000) == "NOK 24 million"
    assert synthesis.money("850000") == "NOK 850,000"
    assert synthesis.money("n/a") is None
    weird = envelope([claim("legal_name", {"odd": 1}), claim("revenue", "abc", section="accounts"),
                      claim("role", "not a dict", section="leadership"), claim("registry_employees", None),
                      {"field": None, "value": None}], changes=[{"change_type": "new_job"}, {"change_type": "new_job"},
                                                              {"change_type": "new_role"}], baseline=False)
    out = synthesis.build(weird)
    assert out["summary"].startswith("Sandnes Elektriske AS is a Norwegian organisation.")
    assert out["what_changed"] == "Since the previous run: 2 new job ads, 1 new role."
    assert synthesis.build(None)["cannot_establish"] == []
    assert synthesis.build({"claims": "garbage"})["summary"]


# --- refresh ------------------------------------------------------------------------------------------------
def job(u, cid, ev):
    return claim("job_posting", {"title": "T", "url": f"https://arbeidsplassen.nav.no/stillinger/stilling/{u}",
                                 "date_posted": "2026-08-01", "valid_through": None, "location": None, "source": "nav"},
                 section="hiring", cid=cid, ev=[ev])


def evid(eid, at):
    return {"id": eid, "source_url": "https://arbeidsplassen.nav.no/x", "final_url": None, "source_class": "official_job_board",
            "retrieved_at": at, "http_status": 200, "content_sha256": None, "snapshot_path": None, "claim_span": "x",
            "extraction_method": "t", "reporting_period": None}


def test_refresh_new_and_closed_job_and_idempotent():
    prev = envelope([job("a", "job-1", "evj-1"), claim("active_job_count", 1, section="hiring")],
                    evidence=[evid("evj-1", "2026-08-01T00:00:00Z"), evid("ev-1", "2026-08-01T00:00:00Z")])
    cur = envelope([job("b", "job-1", "evj-1"), claim("active_job_count", 1, section="hiring")], baseline=False,
                   evidence=[evid("evj-1", RETRIEVED), evid("ev-1", RETRIEVED)])
    changes = refresh.diff(prev, cur)
    by_type = {c["change_type"]: c for c in changes}
    assert set(by_type) == {"new_job", "closed_job"}
    new = by_type["new_job"]
    assert new["field"] == "job_posting" and new["materiality"] == "material" and new["previous_value"] is None
    assert new["current_value"]["url"].endswith("/b") and new["evidence_ids"] == ["evj-1"] and new["previous_evidence_ids"] == []
    assert new["first_observed"] == RETRIEVED and new["last_observed"] == "2026-09-09T10:05:00Z"
    closed = by_type["closed_job"]
    assert closed["previous_value"]["url"].endswith("/a") and closed["current_value"] is None
    assert closed["first_observed"] == "2026-08-01T00:00:00Z" and closed["previous_evidence_ids"] == ["evj-1"]
    assert refresh.diff(cur, copy.deepcopy(cur)) == []
    assert refresh.diff(prev, prev) == []
    assert refresh.diff(None, cur) == []


def test_refresh_filing_website_and_availability():
    prev = envelope([claim("revenue", 10, section="accounts", period="2024"), claim("official_website", None, section="web",
                                                                                     availability="not_available"),
                     claim("website_title", "Old", section="web"), claim("registry_update", None, section="activity",
                                                                          availability="not_available")])
    cur = envelope([claim("revenue", 12, section="accounts", period="2025"),
                    claim("official_website", "https://www.example.no/", section="web"),
                    claim("website_title", None, section="web", availability="blocked"),
                    claim("registry_update", {"date": "2026-01-01", "change_type": "Endring"}, section="activity")], baseline=False)
    types = {(c["field"], c["change_type"], c["materiality"]) for c in refresh.diff(prev, cur)}
    assert types == {("revenue", "new_filing", "material"), ("official_website", "new_website", "material"),
                     ("website_title", "availability_changed", "material"),
                     ("registry_update", "new_registry_update", "minor")}
    same_period = envelope([claim("revenue", 13, section="accounts", period="2025")], baseline=False)
    cur2 = envelope([claim("revenue", 12, section="accounts", period="2025")], baseline=False)
    assert [c["change_type"] for c in refresh.diff(cur2, same_period)] == ["changed_financials"]
    assert refresh.diff(cur, cur) == []
    moved = envelope([claim("official_website", "https://example.com/", section="web")], baseline=False)
    assert [c["change_type"] for c in refresh.diff(cur, moved)] == ["changed_website"]
    assert refresh.diff(cur, envelope([claim("official_website", "http://example.no", section="web")], baseline=False)) == []


def test_load_previous(tmp_path):
    p = tmp_path / "envelopes.jsonl"
    p.write_text('{"organisation_number":"1","run":1}\nnot json\n{"organisation_number":"1","run":2}\n{"organisation_number":"2"}\n',
                 encoding="utf-8")
    out = refresh.load_previous(p)
    assert set(out) == {"1", "2"} and out["1"]["run"] == 2
    assert refresh.load_previous(tmp_path / "missing.jsonl") == {}


def test_synthesis_answers_are_claim_backed_or_explicitly_unanswerable():
    """R10: every answer either cites claim ids that exist on the envelope, or says the evidence cannot answer."""
    env = envelope([
        claim("legal_name", "Sandnes Elektriske AS"), claim("legal_form", "AS"), claim("municipality", "SANDNES"),
        claim("industry_code", "43.210"), claim("industry_label", "Elektrisk installasjonsarbeid"),
        claim("revenue", 24_100_000, section="accounts", period="2025-01-01..2025-12-31"),
        claim("reporting_period", {"from": "2025-01-01", "to": "2025-12-31"}, section="accounts", period="2025-01-01..2025-12-31"),
        claim("accounts_prior_period", {"reporting_period": "2024-01-01..2024-12-31", "revenue": 20_000_000.0, "annual_result": 500_000.0,
                                        "total_assets": None, "equity": None, "total_debt": None, "currency": "NOK"},
              section="accounts", period="2024-01-01..2024-12-31"),
        claim("status_flags", {"bankrupt": False, "liquidating": True, "forced_liquidation": False}),
    ])
    out = synthesis.build(env)
    ids = {c["id"] for c in env["claims"]}
    questions = {a["question"]: a for a in out["answers"]}
    assert len(out["answers"]) >= 10
    for a in out["answers"]:
        if a["answerable"]:
            assert a["answer"] and all(i in ids for i in a["claim_ids"]), a
        else:
            assert a["claim_ids"] == [] and a["answer"] in ("The evidence does not establish this.",
                                                            "First run — there is no previous snapshot to compare.")
    assert questions["Is it growing?"]["answerable"] and "Revenue rose 20% from NOK 20 million (2024) to NOK 24.1 million (2025)" in out["trend"]
    assert questions["Who leads it?"]["answerable"] is False
    assert any("wound up" in f for f in out["risk_flags"]) and "wound up" in out["summary"]
