"""Claim / evidence / error helpers and the envelope validator. Plain dicts, validated with pydantic at the end."""
from __future__ import annotations

import html
import itertools
import json
import re
import threading
from typing import Any, Optional

AVAILABLE = "available"
NOT_AVAILABLE = "not_available"
BLOCKED = "blocked"
NOT_APPLICABLE = "not_applicable"
AMBIGUOUS = "ambiguous"
FAILED = "failed"
STATES = (AVAILABLE, NOT_AVAILABLE, BLOCKED, NOT_APPLICABLE, AMBIGUOUS, FAILED)

SOURCE_CLASSES = ("official_registry", "official_accounts", "official_roles", "official_subunits", "official_updates",
                  "company_owned", "official_job_board", "search_candidate")
SECTIONS = ("identity", "accounts", "leadership", "workplaces", "web", "hiring", "activity")


class IdGen:
    """Per-company id generator so ids are unique inside one envelope."""

    def __init__(self):
        self._counters: dict[str, itertools.count] = {}
        self._lock = threading.Lock()

    def next(self, prefix: str) -> str:
        with self._lock:
            c = self._counters.setdefault(prefix, itertools.count(1))
            return f"{prefix}-{next(c)}"


def new_claim(ids: IdGen, section: str, field: str, value: Any, availability: str, evidence_ids: list[str],
              confidence: float = 1.0, note: Optional[str] = None, reporting_period: Optional[str] = None,
              effective_date: Optional[str] = None, prefix: str = "c") -> dict:
    assert section in SECTIONS, section
    assert availability in STATES, availability
    return {"id": ids.next(prefix), "section": section, "field": field, "value": value, "availability": availability,
            "confidence": round(float(confidence), 3), "evidence_ids": list(evidence_ids),
            "reporting_period": reporting_period, "effective_date": effective_date, "note": note}


def literal_span(raw: str, span: str) -> str:
    """The verbatim form of `span` inside `raw`, so a checker can find the proof in the snapshot.

    Returns `span` itself when it already occurs in `raw`. A value the source escaped (``\\/`` in JSON-LD,
    ``&amp;`` in markup) comes back in the escaped form. A span that is JSON but not a verbatim excerpt of the
    record (a selection of its fields) is replaced by the raw excerpt, at most 300 characters, that starts at the
    first of its fields found in the record and runs over the following fields that fit. Anything else is returned
    unchanged: text cut from a rendered page is checked against the page's visible text, not its markup.
    """
    if not raw or not span or span in raw:
        return span
    for alt in (span.replace("/", "\\/"), html.escape(span, quote=False), html.escape(span)):
        if alt != span and alt in raw:
            return alt
    if span[:1] not in "{[":
        return span
    try:
        obj = json.loads(span)
    except ValueError:
        return span
    leaves: list[tuple[str, Any]] = []

    def walk(o, key=None):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            if key is not None and all(not isinstance(v, (dict, list)) for v in o):
                leaves.append((key, o))            # a list of scalars is one value: "navn":["FIRMA AS"]
            else:
                for v in o:
                    walk(v, key)
        elif key is not None:
            leaves.append((key, o))

    walk(obj)
    hits = []
    for key, val in leaves:
        pat = (re.escape(json.dumps(key.rsplit(".", 1)[-1], ensure_ascii=False)) + r"\s*:\s*"
               + re.escape(json.dumps(val, ensure_ascii=False, separators=(",", ":"))))
        m = re.search(pat, raw)
        if m:
            hits.append((m.start(), m.end()))
    if not hits:
        return span
    start, end = hits[0]                            # the first field named is the anchor
    for a, b in sorted(hits):
        if a >= start and b - start <= 300:
            end = max(end, b)
    return raw[start:end]


def new_evidence(ids: IdGen, fetch, source_class: str, claim_span: str, extraction_method: str,
                 reporting_period: Optional[str] = None, prefix: str = "ev") -> dict:
    """Build an evidence record from a FetchResult. The span is made verbatim against the fetched bytes."""
    assert source_class in SOURCE_CLASSES, source_class
    span = literal_span(getattr(fetch, "text", "") or "", (claim_span or "").strip())   # before any whitespace change
    span = re.sub(r"\s+", " ", span).strip()[:300]
    return {"id": ids.next(prefix), "source_url": fetch.url, "final_url": fetch.final_url or fetch.url,
            "source_class": source_class, "retrieved_at": fetch.retrieved_at, "http_status": fetch.status,
            "content_sha256": fetch.sha256 or None, "snapshot_path": fetch.snapshot_path or None,
            "claim_span": span, "extraction_method": extraction_method, "reporting_period": reporting_period}


def new_error(stage: str, message: str, availability: str, source_url: Optional[str] = None) -> dict:
    assert availability in STATES
    return {"stage": stage, "source_url": source_url, "message": str(message)[:300], "availability": availability}


def state_from_fetch(fetch) -> str:
    """Map a failed FetchResult onto an availability state."""
    if fetch.blocked:
        return BLOCKED
    if fetch.budget_exhausted:
        return NOT_AVAILABLE
    if fetch.status in (404, 410):
        return NOT_AVAILABLE
    return FAILED


# ---------------------------------------------------------------------------------------------------------
# Envelope validation (pydantic) — used by the pipeline before writing and by `signalpost validate`.
try:
    from pydantic import BaseModel, Field, field_validator

    class Claim(BaseModel):
        id: str
        section: str
        field: str
        value: Any
        availability: str
        confidence: float = Field(ge=0, le=1)
        evidence_ids: list[str]
        reporting_period: Optional[str] = None
        effective_date: Optional[str] = None
        note: Optional[str] = None

        @field_validator("availability")
        @classmethod
        def _state(cls, v):
            assert v in STATES, f"bad availability {v}"
            return v

        @field_validator("section")
        @classmethod
        def _section(cls, v):
            assert v in SECTIONS, f"bad section {v}"
            return v

    class Evidence(BaseModel):
        id: str
        source_url: str
        final_url: Optional[str] = None
        source_class: str
        retrieved_at: str
        http_status: Optional[int] = None
        content_sha256: Optional[str] = None
        snapshot_path: Optional[str] = None
        claim_span: str
        extraction_method: str
        reporting_period: Optional[str] = None

        @field_validator("source_class")
        @classmethod
        def _sc(cls, v):
            assert v in SOURCE_CLASSES, f"bad source_class {v}"
            return v

    class Change(BaseModel):
        field: str
        change_type: str
        materiality: str
        previous_value: Any = None
        current_value: Any = None
        first_observed: str
        last_observed: str
        evidence_ids: list[str] = []
        previous_evidence_ids: list[str] = []

    class Error(BaseModel):
        stage: str
        source_url: Optional[str] = None
        message: str
        availability: str

    class Operations(BaseModel):
        requests: int
        runtime_ms: int
        third_party_cost_usd: float
        bytes: int
        budget_exhausted: bool

    class Run(BaseModel):
        run_id: str
        started_at: str
        completed_at: str
        terminal_status: str
        agent_version: str
        previous_run_id: Optional[str] = None
        refresh: dict

    class Envelope(BaseModel):
        organisation_number: str
        schema_version: str
        run: Run
        identity: dict
        sections: dict[str, str]
        claims: list[Claim]
        evidence: list[Evidence]
        changes: list[Change]
        errors: list[Error]
        operations: Operations
        synthesis: dict
        diagnostics: dict = {}

        @field_validator("organisation_number")
        @classmethod
        def _org(cls, v):
            assert re.fullmatch(r"\d{9}", v), "organisation_number must be 9 digits"
            return v

    def validate_envelope(env: dict) -> list[str]:
        """Return a list of problems (empty = valid). Also checks referential integrity of evidence ids."""
        problems: list[str] = []
        try:
            Envelope.model_validate(env)
        except Exception as exc:  # pydantic ValidationError
            problems.append(str(exc)[:500])
        ev_ids = {e["id"] for e in env.get("evidence", [])}
        for c in env.get("claims", []):
            if c.get("availability") == AVAILABLE and not c.get("evidence_ids"):
                problems.append(f"claim {c.get('id')} ({c.get('field')}) is available but has no evidence")
            for eid in c.get("evidence_ids", []):
                if eid not in ev_ids:
                    problems.append(f"claim {c.get('id')} references unknown evidence {eid}")
        for s, st in env.get("sections", {}).items():
            if st not in STATES:
                problems.append(f"section {s} has bad state {st}")
        return problems

except ImportError:  # pragma: no cover
    def validate_envelope(env: dict) -> list[str]:
        return []
