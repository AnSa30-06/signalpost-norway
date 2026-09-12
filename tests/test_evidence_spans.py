"""The evidence contract: a claim_span is verbatim in the bytes it cites (docs/DATA_SCHEMA.md)."""
from __future__ import annotations

import json

from signalpost.models import IdGen, new_evidence
from signalpost.net import FetchResult
from signalpost.registry import Official


def fetch(text: str) -> FetchResult:
    return FetchResult(url="https://data.brreg.no/x", final_url="https://data.brreg.no/x", status=200, body=text.encode(),
                       text=text, retrieved_at="2026-09-13T00:00:00Z", sha256="ab" * 32, snapshot_path="snapshots/x.json")


def squash(t: str) -> str:
    return "".join((t or "").split())


def test_a_non_breaking_space_in_the_record_does_not_break_the_verbatim_lookup():
    raw = '{"navn":"X AS","vedtektsfestetFormaal":["Salg av varer","og tjenester"]}'
    ev = new_evidence(IdGen(), fetch(raw), "official_registry",
                      json.dumps({"vedtektsfestetFormaal": ["Salg av varer", "og tjenester"]}, ensure_ascii=False, separators=(",", ":")),
                      "brreg_enheter_api")
    assert ev["claim_span"].startswith('"vedtektsfestetFormaal"') and squash(ev["claim_span"]) in squash(raw)


def test_absent_registry_flags_are_not_available_not_false():
    """A record without konkurs/underAvvikling/MVA fields says nothing; the old code published False."""
    org = "829255812"
    body = json.dumps({"organisasjonsnummer": org, "navn": "TOM AS", "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
                       "forretningsadresse": {"adresse": ["Gata 1"], "postnummer": "0150", "poststed": "OSLO", "kommune": "OSLO", "kommunenummer": "0301"},
                       "registreringsdatoEnhetsregisteret": "2020-01-01"})

    class S:
        def get(self, url, company, kind="json", **kw):
            return fetch(body) if url.endswith(org) else FetchResult(url=url, final_url=url, status=404, error="http_404", retrieved_at="2026-09-13T00:00:00Z")

        def remaining(self, company):
            return 99

    o = Official(S(), IdGen(), org, {"organisation_number": org, "name": "TOM AS", "legal_form": "AS"})
    o.identity()
    by = {c["field"]: c for c in o.claims}
    assert by["vat_registered"]["availability"] == "not_available" and by["vat_registered"]["value"] is None
    assert by["status_flags"]["availability"] == "not_available" and by["status_flags"]["value"] is None
    assert by["legal_name"]["availability"] == "available"
    for c in o.claims:
        if c["availability"] == "available":
            for eid in c["evidence_ids"]:
                span = next(e["claim_span"] for e in o.evidence if e["id"] == eid)
                assert squash(span) in squash(body), (c["field"], span)
