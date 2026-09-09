"""Offline tests for the session guard, the envelope validator, input parsing and the registry normaliser."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from signalpost.cli import read_inputs
from signalpost.models import IdGen, new_claim, new_evidence, validate_envelope
from signalpost.net import FetchResult, UnsafeURL, assert_public_url
from signalpost.registry import Official


class FakeSession:
    def __init__(self, responses: dict[str, tuple[int, str]]):
        self.responses = responses
        self.log = []
        self.calls = 0

    def get(self, url, company, kind="html", **kw):
        self.calls += 1
        status, body = self.responses.get(url, (404, ""))
        r = FetchResult(url=url, final_url=url, status=status, body=body.encode("utf-8"), text=body, retrieved_at="2026-09-09T10:00:00Z",
                        sha256="ab" * 32, snapshot_path="snapshots/x.json", content_type="application/json")
        if status >= 400:
            r.error = f"http_{status}"
        return r

    def remaining(self, company):
        return 99


@pytest.mark.parametrize("bad", ["ftp://example.no/", "http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/x", "http://[::1]/",
                                 "http://user:pw@example.no/", "http://intranet.local/"])
def test_unsafe_urls_rejected(bad):
    with pytest.raises(UnsafeURL):
        assert_public_url(bad)


def test_inputs_parse_jsonl_and_text(tmp_path: Path):
    p = tmp_path / "in.jsonl"
    p.write_text('{"organisation_number":"938702675"}\n810034882\n938702675\nnot-a-number\n', encoding="utf-8")
    assert read_inputs(p) == ["938702675", "810034882"]


def test_validator_flags_available_claim_without_evidence():
    ids = IdGen()
    env = {"organisation_number": "938702675", "schema_version": "1.0",
           "run": {"run_id": "t", "started_at": "x", "completed_at": "y", "terminal_status": "completed", "agent_version": "1", "refresh": {"baseline": True}},
           "identity": {}, "sections": {"identity": "available"}, "claims": [new_claim(ids, "identity", "legal_name", "X AS", "available", [])],
           "evidence": [], "changes": [], "errors": [],
           "operations": {"requests": 1, "runtime_ms": 1, "third_party_cost_usd": 0.0, "bytes": 0, "budget_exhausted": False}, "synthesis": {}}
    problems = validate_envelope(env)
    assert any("no evidence" in p for p in problems)


def test_registry_accounts_404_is_not_available_not_zero():
    org = "999999999"
    s = FakeSession({f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}": (404, "")})
    o = Official(s, IdGen(), org, {"legal_form": "AS"})
    o.accounts()
    rev = [c for c in o.claims if c["field"] == "revenue"][0]
    assert rev["availability"] == "not_available" and rev["value"] is None
    assert o.sections["accounts"] == "not_available"


def test_registry_accounts_parse_and_period():
    org = "938702675"
    body = json.dumps([{"regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "valuta": "NOK", "regnskapstype": "SELSKAP",
                        "resultatregnskapResultat": {"aarsresultat": 853000000.0, "ordinaertResultatFoerSkattekostnad": 846000000.0,
                                                     "driftsresultat": {"driftsresultat": 876000000.0, "driftsinntekter": {"sumDriftsinntekter": 914000000.0}}},
                        "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 968000000.0}, "gjeldOversikt": {"sumGjeld": 3217000000.0}},
                        "eiendeler": {"sumEiendeler": 4184000000.0}}])
    s = FakeSession({f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}": (200, body)})
    o = Official(s, IdGen(), org, {"legal_form": "ASA"})
    o.accounts()
    by = {c["field"]: c for c in o.claims}
    assert by["revenue"]["value"] == 914000000.0 and by["revenue"]["reporting_period"] == "2025-01-01..2025-12-31"
    assert by["revenue"]["evidence_ids"] and o.evidence[0]["claim_span"]
    assert by["total_debt"]["value"] == 3217000000.0


def test_registry_roles_skip_birthdates_and_resigned():
    org = "938702675"
    body = json.dumps({"rollegrupper": [{"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "sistEndret": "2020-09-14",
                                         "roller": [{"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "person": {"navn": {"fornavn": "Amund", "etternavn": "Tøftum"}, "fodselsdato": "1970-01-01"}, "fratraadt": False},
                                                    {"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "person": {"navn": {"fornavn": "Old", "etternavn": "Boss"}}, "fratraadt": True}]}]})
    s = FakeSession({f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller": (200, body)})
    o = Official(s, IdGen(), org, {})
    o.roles()
    roles = [c for c in o.claims if c["field"] == "role"]
    assert len(roles) == 1 and roles[0]["value"]["name"] == "Amund Tøftum"
    assert "1970" not in json.dumps(o.claims) and "1970" not in json.dumps(o.evidence)


def test_zero_subunits_is_explicit_zero():
    org = "938702675"
    s = FakeSession({f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=200": (200, json.dumps({"page": {"totalElements": 0}}))})
    o = Official(s, IdGen(), org, {})
    o.workplaces()
    cnt = [c for c in o.claims if c["field"] == "workplace_count"][0]
    assert cnt["value"] == 0 and cnt["availability"] == "available" and "zero" in cnt["note"]
