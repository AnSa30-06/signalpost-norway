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
    html = ("<html><head><title>AF Gruppen</title></head><body><p>AF Gruppen bygger i Oslo.</p>"
            "<footer>Kontakt: post@afgruppen.no, tlf +47 22 89 11 00</footer></body></html>")
    profile = {"organisation_number": "938702675", "name": "AF GRUPPEN ASA", "municipality": "OSLO",
               "registry": {"forretningsadresse": {"adresse": ["Standardveien 1"], "postnummer": "0581", "poststed": "OSLO", "kommune": "OSLO"}}}
    res = identity.assess(profile, _page("https://www.afgruppen.no/", html))
    assert res["status"] == "exact"
    assert any(r.startswith("domain_is_legal_name:") for r in res["reasons"])


def test_hosting_placeholder_is_never_the_official_website():
    """paulsendrift.no served "Hosted By One.com" and was published as PAULSEN DRIFT AS on the domain name alone."""
    from signalpost import identity
    html = "<html><head><title>Hosted By One.com | Webhosting made simple</title></head><body><p>Webhosting made simple</p></body></html>"
    profile = {"organisation_number": "925741787", "name": "PAULSEN DRIFT AS", "municipality": "ALTA",
               "registry": {"forretningsadresse": {"adresse": ["Sagasletta 4"], "postnummer": "9517", "poststed": "ALTA", "kommune": "ALTA"}}}
    res = identity.assess(profile, _page("https://paulsendrift.no/", html))
    assert res["status"] != "exact" and res["score"] <= 0.3


def test_foreign_namesake_on_a_matching_domain_is_not_exact():
    """hoainvest.com spells HOA INVEST AS exactly and is a California firm: nothing on it ties it to Norway."""
    from signalpost import identity
    html = ("<html><head><title>HOA Invest</title></head><body><p>HOA Invest serves homeowner associations."
            "</p><footer>Irvine, California. Contact info@hoainvest.com</footer></body></html>")
    profile = {"organisation_number": "930312312", "name": "HOA INVEST AS", "municipality": "BÆRUM",
               "registry": {"forretningsadresse": {"adresse": ["Hoslejordet 10"], "postnummer": "1362", "poststed": "HOSLE", "kommune": "BÆRUM"}}}
    res = identity.assess(profile, _page("https://hoainvest.com/", html))
    assert res["status"] != "exact"
    assert not any(r.startswith("domain_is_legal_name:") for r in res["reasons"])


def test_name_only_in_the_hostname_never_counts_as_the_name_being_present():
    """The name must appear in the page's own content; a domain string is not the page identifying itself."""
    from signalpost import identity
    html = "<html><head><title>Velkommen</title></head><body><p>Vi selger blomster. Ring +47 22 00 00 00.</p></body></html>"
    profile = {"organisation_number": "928608360", "name": "LILLY BLOMSTER AS", "municipality": "OSLO",
               "registry": {"forretningsadresse": {"adresse": ["Storgata 1"], "postnummer": "0155", "poststed": "OSLO", "kommune": "OSLO"}}}
    res = identity.assess(profile, _page("https://lillyblomster.no/", html))
    assert res["status"] != "exact"


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


def test_a_different_stated_org_number_blocks_verification():
    """industrifinans.no carries the right brand and the exact registered street, and states another entity's number."""
    from signalpost import identity
    html = ("<html><head><title>Industrifinans</title></head><body><p>Industrifinans</p>"
            "<footer>Oscars gt. 30, 0352 Oslo. Org.nummer: 993 075 558</footer></body></html>")
    profile = {"organisation_number": "924351020", "name": "INDUSTRIFINANS AS", "municipality": "OSLO",
               "registry": {"forretningsadresse": {"adresse": ["Oscars gate 30"], "postnummer": "0352", "poststed": "OSLO", "kommune": "OSLO"}}}
    res = identity.assess(profile, _page("https://www.industrifinans.no/", html))
    assert res["status"] != "exact"
    assert "name_match_but_other_org_number" in res["reasons"]


def test_our_own_org_number_still_wins_over_the_mismatch_rule():
    from signalpost import identity
    html = ("<html><head><title>Elinsta AS</title></head><body><p>Elinsta AS</p>"
            "<footer>Org.nr.: 918453547. Levert av Nettbyraa AS, org.nr 999 888 777</footer></body></html>")
    profile = {"organisation_number": "918453547", "name": "ELINSTA AS", "municipality": "LØRENSKOG", "registry": {}}
    res = identity.assess(profile, _page("https://elinsta.no/", html))
    assert res["status"] == "exact" and res["reasons"] == ["org_number_on_page"]


def test_other_org_numbers_helper():
    from signalpost.identity import other_org_numbers
    assert other_org_numbers("Org.nummer: 993 075 558", "924351020") == ["993075558"]
    assert other_org_numbers("Org.nr 924 351 020", "924351020") == []
    assert other_org_numbers("Ring oss paa 993 075 558", "924351020") == []   # unlabelled digits are not an org number


def test_registry_email_domain_becomes_a_candidate():
    from signalpost.discovery import email_domain_candidate, candidates
    prof = {"organisation_number": "851217932", "name": "HETA AS", "registry": {"epostadresse": "post@heta.no"}}
    c = email_domain_candidate(prof)
    assert c["url"] == "https://heta.no/" and c["origin"] == "registry_email"
    assert any(x["origin"] == "registry_email" for x in candidates(prof, None))


def test_consumer_mail_domains_are_not_candidates():
    from signalpost.discovery import email_domain_candidate
    for addr in ("ola@gmail.com", "kari@hotmail.no", "post@online.no", "notanemail"):
        assert email_domain_candidate({"registry": {"epostadresse": addr}}) is None
