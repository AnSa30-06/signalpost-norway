"""Revision 4: Brave search discovery (transient), claim-level provenance, and the run's time budget."""
from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from signalpost import discovery, net, pipeline, refresh, registry
from signalpost.cli import load_dotenv
from signalpost.models import NOT_CHECKED, IdGen
from signalpost.net import FetchResult, Session
from signalpost.registry import Official

PROFILE = {"organisation_number": "914922941", "name": "SANDNES ELEKTRISKE AS", "municipality": "SANDNES"}


@pytest.fixture(autouse=True)
def clean_search(monkeypatch):
    for k in discovery.BRAVE_KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    discovery._search["queries"] = 0
    discovery.set_search_limit(2500)
    yield
    discovery._search["queries"] = 0


class BraveFake:
    """Answers Brave queries in order with canned result lists; records every call."""

    def __init__(self, *result_lists):
        self.results = list(result_lists)
        self.calls = []

    def get(self, url, company, kind="html", **kw):
        self.calls.append({"url": url, **kw})
        urls = self.results.pop(0) if self.results else []
        body = json.dumps({"web": {"results": [{"url": u, "title": "t", "description": "d"} for u in urls]}})
        return FetchResult(url=url, final_url=url, status=200, body=body.encode(), text=body, retrieved_at="2026-09-28T00:00:00Z")


def test_the_key_is_read_from_builderrs_variable_name_first(monkeypatch):
    assert not discovery.brave_enabled()
    monkeypatch.setenv("BRAVE_API_KEY", "old-name")
    assert discovery.brave_key() == "old-name"
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "starter-kit-name")
    assert discovery.brave_key() == "starter-kit-name"


def test_the_query_quotes_the_legal_name_and_carries_the_organisation_number():
    assert discovery.search_queries(PROFILE) == ['"SANDNES ELEKTRISKE AS" 914922941', '"SANDNES ELEKTRISKE AS" SANDNES']


def test_search_candidates_keep_only_name_hosts_skip_directories_and_never_store(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    fake = BraveFake(["https://www.proff.no/selskap/x", "https://www.purehelp.no/x", "https://byggmester-oslo.no/om",
                      "https://www.sandneselektriske.no/kontakt", "https://www.companywall.no/x", "https://www.facebook.com/x",
                      "https://nyheter.example.no/artikkel", "https://sandneselektriske.no/", "https://tredje.no/"])
    cands, spent = discovery.brave_candidates(PROFILE, fake)
    assert spent == 1
    # the news article and the unrelated firm print the number too; only the name-bearing host is a candidate
    assert [c["url"] for c in cands] == ["https://www.sandneselektriske.no/kontakt"]
    assert all(c["origin"] == "brave" for c in cands)
    call = fake.calls[0]
    assert call["store"] is False and call["robots"] is False
    assert call["headers"]["X-Subscription-Token"] == "k"
    assert "country=no" in call["url"] and "914922941" in call["url"]


def test_the_second_query_runs_only_when_the_first_gives_nothing(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    fake = BraveFake(["https://www.proff.no/x", "https://www.1881.no/x"], ["https://sandneselektriske.no/"])
    cands, spent = discovery.brave_candidates(PROFILE, fake)
    assert spent == 2 and [c["url"] for c in cands] == ["https://sandneselektriske.no/"]
    fake = BraveFake(["https://sandneselektriske.no/"], ["https://never-asked.no/"])
    cands, spent = discovery.brave_candidates(PROFILE, fake)
    assert spent == 1 and len(fake.calls) == 1


def test_domains_already_probed_are_excluded(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    fake = BraveFake(["https://sandneselektriske.no/", "https://elektriske-sandnes.com/"])
    cands, _ = discovery.brave_candidates(PROFILE, fake, exclude={"sandneselektriske.no"})
    assert [c["url"] for c in cands] == ["https://elektriske-sandnes.com/"]


def test_the_municipality_and_generic_words_do_not_make_a_host_a_candidate(monkeypatch):
    """"sandnes" is a word of the name but also the municipality: sandnesavis.no must not qualify."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    fake = BraveFake(["https://www.sandnesavis.no/nyhet/123", "https://www.holding.no/"])
    cands, spent = discovery.brave_candidates(PROFILE, fake)
    assert cands == [] and spent == 2


def test_the_run_wide_query_cap_holds(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    discovery.set_search_limit(1)
    fake = BraveFake(["https://www.proff.no/x"], ["https://sandneselektriske.no/"])
    cands, spent = discovery.brave_candidates(PROFILE, fake)
    assert spent == 1 and cands == [] and len(fake.calls) == 1
    assert discovery.search_stats()["queries"] == 1 and discovery.search_stats()["cost_usd"] == pytest.approx(0.005)


def test_no_key_means_no_query(monkeypatch):
    fake = BraveFake(["https://a.no/"])
    assert discovery.brave_candidates(PROFILE, fake) == ([], 0) and fake.calls == []


def test_a_transient_fetch_writes_no_snapshot_and_logs_no_query(tmp_path, monkeypatch):
    """Brave's terms forbid keeping results: the response body must not reach snapshots/ or the request log."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"web": {"results": [{"url": "https://a.no/"}]}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(net, "assert_public_url", lambda u: None)
    s = Session(tmp_path)
    url = f"http://127.0.0.1:{srv.server_address[1]}/res/v1/web/search?q=%22SECRET+QUERY%22"
    r = s.get(url, company="914922941", kind="json", robots=False, store=False)
    srv.shutdown()
    assert r.ok and r.json()["web"]["results"][0]["url"] == "https://a.no/"
    assert r.snapshot_path in ("", None) and not list((tmp_path / "snapshots").iterdir())
    s.dump_log()
    log = (tmp_path / "requests.jsonl").read_text(encoding="utf-8")
    assert "SECRET" not in log and "/res/v1/web/search" in log


def test_every_claim_carries_its_own_proof():
    claims = [{"id": "c-1", "field": "social_profile", "evidence_ids": ["evs-4"]}, {"id": "c-2", "field": "x", "evidence_ids": []}]
    evidence = [{"id": "evs-4", "source_url": "https://a.no/", "retrieved_at": "2026-09-28T00:00:00Z", "content_sha256": "ab" * 32,
                 "claim_span": "https://www.facebook.com/a/", "source_class": "company_owned"}]
    pipeline.attach_provenance(claims, evidence)
    assert claims[0]["source_url"] == "https://a.no/" and claims[0]["content_sha256"] == "ab" * 32
    assert claims[0]["claim_span"] == "https://www.facebook.com/a/" and claims[0]["retrieved_at"] and claims[0]["source_class"]
    assert all(claims[1][k] is None for k in pipeline.PROVENANCE_KEYS)


def test_the_paced_filing_years_slot_is_refused_after_the_cutoff(monkeypatch):
    monkeypatch.setattr(registry, "HISTORY_CUTOFF", [time.monotonic() - 1])

    class NoCall:
        def get(self, *a, **k):
            raise AssertionError("the endpoint must not be called after the cutoff")

    o = Official(NoCall(), IdGen(), "914922941", {"legal_form": "AS"})
    o.filing_years()
    c = next(c for c in o.claims if c["field"] == "accounts_filing_years")
    assert c["availability"] == "not_available" and c["note"].startswith(NOT_CHECKED)


def test_a_step_skipped_for_time_is_not_a_change():
    prev = {"run": {"completed_at": "2026-09-27T00:00:00Z"}, "evidence": [{"id": "ev-1", "retrieved_at": "2026-09-27T00:00:00Z"}],
            "claims": [{"id": "c-1", "section": "accounts", "field": "accounts_filing_years", "value": ["2024", "2025"],
                        "availability": "available", "evidence_ids": ["ev-1"]}]}
    skipped = {"run": {"completed_at": "2026-09-28T00:00:00Z"}, "evidence": [],
               "claims": [{"id": "c-1", "section": "accounts", "field": "accounts_filing_years", "value": None,
                           "availability": "not_available", "evidence_ids": [], "note": f"{NOT_CHECKED}: time budget"}]}
    assert refresh.diff(prev, skipped) == []
    lost = json.loads(json.dumps(skipped))
    lost["claims"][0]["note"] = "filing-years endpoint failed: http_500"
    assert [c["change_type"] for c in refresh.diff(prev, lost)] == ["availability_changed"]


def test_the_key_file_never_overrides_a_variable_already_set(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nBRAVE_SEARCH_API_KEY='from-file'\nSIGNALPOST_BRAVE_QPS=5\n", encoding="utf-8")
    monkeypatch.setenv("SIGNALPOST_BRAVE_QPS", "2")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    assert load_dotenv([f, tmp_path / "missing.env"]) == ["BRAVE_SEARCH_API_KEY"]
    import os
    assert os.environ["BRAVE_SEARCH_API_KEY"] == "from-file" and os.environ["SIGNALPOST_BRAVE_QPS"] == "2"
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY")


def test_the_real_key_file_is_git_ignored():
    root = Path(__file__).resolve().parent.parent
    assert ".env" in (root / ".gitignore").read_text(encoding="utf-8").split()
    assert "BRAVE_SEARCH_API_KEY=\n" in (root / ".env.example").read_text(encoding="utf-8")
