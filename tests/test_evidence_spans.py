"""The evidence contract: a claim_span is verbatim in the bytes it cites (docs/DATA_SCHEMA.md)."""
from __future__ import annotations

import json

from signalpost.models import IdGen, literal_span, new_evidence
from signalpost.net import FetchResult
from signalpost.registry import Official
from signalpost.site import _addr_text


def fetch(text: str) -> FetchResult:
    return FetchResult(url="https://data.brreg.no/x", final_url="https://data.brreg.no/x", status=200, body=text.encode(),
                       text=text, retrieved_at="2026-09-13T00:00:00Z", sha256="ab" * 32, snapshot_path="snapshots/x.json")


def squash(t: str) -> str:
    return "".join((t or "").split())


def test_a_non_breaking_space_in_the_record_does_not_break_the_verbatim_lookup():
    raw = '{"navn":"X AS","vedtektsfestetFormaal":["Salg av varer","og tjenester"]}'
    ev = new_evidence(IdGen(), fetch(raw), "official_registry",
                      json.dumps({"vedtektsfestetFormaal": ["Salg av varer", "og tjenester"]}, ensure_ascii=False, separators=(",", ":")),
                      "brreg_enheter_api")
    assert ev["claim_span"].startswith('"vedtektsfestetFormaal"') and squash(ev["claim_span"]) in squash(raw)


def test_a_page_that_writes_a_as_a_unicode_escape_yields_the_pages_own_form():
    """Squarespace JSON-LD: "address":"H\\u00E5landsvegen 33 \\nBryne". The span must be findable in those bytes."""
    raw = r'{"@type":"LocalBusiness","address":"H\u00E5landsvegen 33 \nBryne, Rogaland, 4344\nNorway"}'
    assert literal_span(raw, "Hålandsvegen 33") == r"H\u00E5landsvegen 33"
    assert literal_span(raw, "Bryne, Rogaland") == "Bryne, Rogaland"                    # plain text stays plain
    assert _addr_text("Hålandsvegen 33 \nBryne, Rogaland, 4344\nNorway") == "Hålandsvegen 33, Bryne, Rogaland, 4344, Norway"


def test_absent_registry_fields_are_not_available_not_false_or_borrowed():
    """A record without konkurs/underAvvikling/MVA fields says nothing (the old code published False); a foreign
    business address has no municipality (the old code borrowed the universe row's value and cited the record)."""
    org = "829255812"
    body = json.dumps({"organisasjonsnummer": org, "navn": "TOM AS", "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
                       "forretningsadresse": {"land": "Danmark", "landkode": "DK", "adresse": ["Bredgade 30"], "poststed": "DK-1260 KØBENHAVN K"},
                       "registreringsdatoEnhetsregisteret": "2020-01-01"})

    class S:
        def get(self, url, company, kind="json", **kw):
            return fetch(body) if url.endswith(org) else FetchResult(url=url, final_url=url, status=404, error="http_404", retrieved_at="2026-09-13T00:00:00Z")

        def remaining(self, company):
            return 99

    o = Official(S(), IdGen(), org, {"organisation_number": org, "name": "TOM AS", "legal_form": "AS", "municipality": "OSLO"})
    o.identity()
    by = {c["field"]: c for c in o.claims}
    assert by["vat_registered"]["availability"] == "not_available" and by["vat_registered"]["value"] is None
    assert by["status_flags"]["availability"] == "not_available" and by["status_flags"]["value"] is None
    assert by["municipality"]["availability"] == "not_available" and by["municipality"]["value"] is None
    assert by["legal_name"]["availability"] == "available"
    assert by["business_address"]["value"]["country"] == "DK"
    for c in o.claims:
        if c["availability"] == "available":
            for eid in c["evidence_ids"]:
                span = next(e["claim_span"] for e in o.evidence if e["id"] == eid)
                assert squash(span) in squash(body), (c["field"], span)
