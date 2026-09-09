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


def _page(url, html):
    return FetchResult(url=url, final_url=url, status=200, body=html.encode(), text=html,
                       retrieved_at="2026-09-09T10:00:00Z", sha256="ef" * 32, snapshot_path="snapshots/p.html",
                       content_type="text/html; charset=utf-8")


def test_domain_spelling_a_multiword_name_is_corroboration():
    """afgruppen.no for "AF GRUPPEN ASA": the company registered a domain that spells out its legal name."""
    from signalpost import identity
    html = "<html><head><title>AF Gruppen</title></head><body><p>AF Gruppen bygger i Oslo.</p></body></html>"
    profile = {"organisation_number": "938702675", "name": "AF GRUPPEN ASA", "municipality": "OSLO",
               "registry": {"forretningsadresse": {"adresse": ["Standardveien 1"], "postnummer": "0581", "poststed": "OSLO", "kommune": "OSLO"}}}
    res = identity.assess(profile, _page("https://www.afgruppen.no/", html))
    assert res["status"] == "exact"
    assert any(r.startswith("domain_is_legal_name:") for r in res["reasons"])


def test_single_word_name_on_a_matching_domain_is_not_enough():
    """vit.no for "VIT AS" could be any namesake: a one-word name still needs a postcode or street on the page."""
    from signalpost import identity
    html = "<html><head><title>VIT</title></head><body><p>VIT leverer tjenester i Oslo.</p></body></html>"
    profile = {"organisation_number": "812686542", "name": "VIT AS", "municipality": "OSLO",
               "registry": {"forretningsadresse": {"adresse": ["Kongens gate 2"], "postnummer": "0153", "poststed": "OSLO", "kommune": "OSLO"}}}
    res = identity.assess(profile, _page("https://vit.no/", html))
    assert res["status"] != "exact" and res["score"] < 0.9


def test_tls_errors_are_named_not_lumped_into_network():
    from signalpost.net import classify_tls_error
    assert classify_tls_error("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain").startswith("tls_intercepted")
    assert classify_tls_error("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate").startswith("tls_incomplete_chain")
    assert classify_tls_error("hostname mismatch, certificate is not valid for 'x.no'").startswith("tls_hostname_mismatch")
    assert classify_tls_error("connection reset by peer") is None


def test_zero_subunits_is_explicit_zero():
    org = "938702675"
    s = FakeSession({f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=200": (200, json.dumps({"page": {"totalElements": 0}}))})
    o = Official(s, IdGen(), org, {})
    o.workplaces()
    cnt = [c for c in o.claims if c["field"] == "workplace_count"][0]
    assert cnt["value"] == 0 and cnt["availability"] == "available" and "zero" in cnt["note"]
