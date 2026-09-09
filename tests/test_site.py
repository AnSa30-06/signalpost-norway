"""Offline tests for identity.assess, discovery.candidates and site.crawl (agent A modules)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from signalpost import discovery, identity, site
from signalpost.net import FetchResult

FIX = Path(__file__).parent / "fixtures"
ORIGIN = "https://sandneselektriske.no"
PROFILE = {
    "organisation_number": "987654321", "name": "SANDNES ELEKTRISKE AS", "legal_form": "AS", "municipality": "SANDNES",
    "registry": {"hjemmeside": "sandneselektriske.no",
                 "forretningsadresse": {"adresse": ["Storgata 12"], "postnummer": "4306", "poststed": "SANDNES",
                                        "kommune": "SANDNES"}},
}


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def page(url: str, body: str, status: int = 200) -> FetchResult:
    b = body.encode("utf-8")
    return FetchResult(url=url, final_url=url, status=status, body=b, text=body, sha256=hashlib.sha256(b).hexdigest(),
                       retrieved_at="2026-09-09T00:00:00Z", snapshot_path=f"snapshots/{hashlib.sha256(b).hexdigest()}.html")


class FakeSession:
    """Same surface as signalpost.net.Session that the modules use, backed by fixture strings."""

    def __init__(self, pages: dict, cap: int = 22, sitemaps=None):
        self.pages, self.cap, self.sitemaps, self.calls = pages, cap, list(sitemaps or []), []

    def get(self, url, company, kind="html", **kw):
        self.calls.append(url)
        if len(self.calls) > self.cap:
            return FetchResult(url=url, final_url=url, error="request_budget_exhausted", budget_exhausted=True,
                               retrieved_at="2026-09-09T00:00:00Z")
        body = self.pages.get(url)
        if body is None:
            return FetchResult(url=url, final_url=url, status=404, error="http_404", retrieved_at="2026-09-09T00:00:00Z")
        return page(url, body)

    def robots_allowed(self, url, company):
        return True

    def sitemaps_from_robots(self, origin):
        return self.sitemaps

    def remaining(self, company):
        return self.cap - len(self.calls)


SITE = {ORIGIN + "/": fx("home.html"), ORIGIN + "/team/": fx("team.html"), ORIGIN + "/karriere/": fx("karriere.html"),
        ORIGIN + "/nyheter/": fx("nyheter.html"), ORIGIN + "/feed.xml": fx("feed.xml"),
        ORIGIN + "/sitemap.xml": fx("sitemap.xml")}


def by_field(res, field):
    return [c for c in res["claims"] if c["field"] == field]


# ---- identity ---------------------------------------------------------------------------------------------
def test_org_number_with_spaces_is_exact():
    r = identity.assess(PROFILE, page(ORIGIN + "/", fx("home.html")))
    assert r["score"] == 1.0 and r["status"] == "exact"
    assert r["reasons"][0] == "org_number_on_page"
    assert "987 654 321" in r["claim_span"]
    assert r["public_brand"] == "Sandnes Elektriske"
    assert "Sandnes Elektriske AS" in r["aliases"]


def test_org_number_variants():
    for txt in ("NO 987 654 321 MVA", "987.654.321", "Orgnr 987654321"):
        r = identity.assess(PROFILE, page("https://x.no/", f"<html><body><p>{txt}</p></body></html>"))
        assert r["score"] == 1.0, txt
    r = identity.assess(PROFILE, page("https://x.no/", "<html><body><p>19876543210</p></body></html>"))
    assert r["score"] < 1.0  # digits inside a longer number do not count


def test_name_and_postcode_is_exact():
    html = ("<html><head><title>Sandnes Elektriske AS – elektriker</title></head>"
            "<body><footer>Storgata 12, 4306 Sandnes</footer></body></html>")
    r = identity.assess(PROFILE, page(ORIGIN + "/", html))
    assert r["score"] == 0.95 and r["status"] == "exact"
    assert r["reasons"][0] == "name_and_address" and "postcode:4306" in r["reasons"]
    assert r["claim_span"]


def test_name_only_is_review():
    html = "<html><head><title>Sandnes Elektriske</title></head><body><p>Velkommen</p></body></html>"
    r = identity.assess(PROFILE, page("https://example.org/", html))
    assert r["score"] == 0.8 and r["status"] == "review" and r["reasons"] == ["name_only"]


def test_parked_page_rejected():
    r = identity.assess(PROFILE, page(ORIGIN + "/", fx("parked.html")))
    assert r["score"] == 0.1 and r["status"] == "related_or_uncertain" and "parked" in r["reasons"]


def test_partial_name_is_uncertain():
    html = "<html><head><title>Elektriske tjenester</title></head><body><p>Hei</p></body></html>"
    r = identity.assess(PROFILE, page("https://example.org/", html))
    assert r["score"] == 0.3 and r["status"] == "related_or_uncertain"


# ---- discovery --------------------------------------------------------------------------------------------
def test_domain_guesses_for_sandnes_elektriske():
    guesses = discovery.domain_guesses("SANDNES ELEKTRISKE AS")
    assert guesses[:2] == ["https://sandneselektriske.no/", "https://sandnes-elektriske.no/"]
    assert len(guesses) <= 4 and guesses[0].endswith(".no/")
    assert discovery.domain_guesses("Eiendom Holding AS") == []  # generic-only names get no guesses
    assert discovery.domain_guesses("Norge AS") == []


def test_candidates_registry_first_and_deduped(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    s = FakeSession({})
    cands = discovery.candidates(PROFILE, s)
    assert cands[0] == {"url": "https://sandneselektriske.no/", "origin": "registry",
                        "note": "hjemmeside field in Enhetsregisteret"}
    urls = [c["url"] for c in cands]
    assert urls.count("https://sandneselektriske.no/") == 1  # the guess duplicating the registry domain is dropped
    assert "https://sandnes-elektriske.no/" in urls
    assert all(c["origin"] in ("registry", "domain_guess") for c in cands) and not s.calls
    blocked = dict(PROFILE, registry={"hjemmeside": "https://www.proff.no/selskap/x"})
    assert all(c["origin"] != "registry" for c in discovery.candidates(blocked, s))


# ---- site: pure helpers ---------------------------------------------------------------------------------
def test_social_normalisation():
    ns = site.normalise_social
    assert ns("https://www.facebook.com/sharer/sharer.php?u=https://x.no/") is None
    assert ns("https://twitter.com/intent/tweet?text=hei") is None
    assert ns("https://www.linkedin.com/in/ola-nordmann/") is None
    assert ns("https://www.youtube.com/watch?v=abc") is None
    assert ns("https://no.linkedin.com/company/sandnes-elektriske/about/") == {
        "platform": "linkedin", "url": "https://linkedin.com/company/sandnes-elektriske"}
    assert ns("https://www.facebook.com/sandneselektriske/")["url"] == "https://facebook.com/sandneselektriske"
    assert ns("https://twitter.com/sandnesel")["url"] == "https://x.com/sandnesel"
    assert ns("https://www.youtube.com/@sandnes")["url"] == "https://youtube.com/@sandnes"
    assert ns("https://www.tiktok.com/@sandnes")["platform"] == "tiktok"


def test_phone_and_date_helpers():
    assert site.normalise_phone("+47 51 12 34 56") == "+4751123456"
    assert site.normalise_phone("0047 51123456") == "+4751123456"
    assert site.normalise_phone("123") is None
    assert site.iso_date("Fri, 05 Sep 2026 10:00:00 +0200") == "2026-09-05"
    assert site.iso_date("12.08.2026") == "2026-08-12"
    assert site.iso_date("2026-06-15T09:00:00+02:00") == "2026-06-15"
    assert site.iso_date("no date here") is None


# ---- site: crawl ------------------------------------------------------------------------------------------
@pytest.fixture
def crawled():
    s = FakeSession(SITE)
    home = page(ORIGIN + "/", fx("home.html"))
    ident = identity.assess(PROFILE, home)
    return site.crawl(PROFILE, s, home, ident, page_budget=6), s


def test_crawl_extracts_everything(crawled):
    res, s = crawled
    assert all(e["availability"] != "failed" for e in res["errors"]), res["errors"]
    web = by_field(res, "official_website")[0]
    assert web["value"] == ORIGIN + "/" and web["confidence"] == 1.0 and web["section"] == "web"
    assert by_field(res, "public_brand")[0]["value"] == "Sandnes Elektriske"
    assert by_field(res, "website_description")[0]["value"].startswith("Sandnes Elektriske leverer")
    socials = {c["value"]["platform"]: c["value"]["url"] for c in by_field(res, "social_profile")}
    assert socials["linkedin"] == "https://linkedin.com/company/sandnes-elektriske"   # JSON-LD sameAs
    assert socials["instagram"] == "https://instagram.com/sandneselektriske"           # JSON-LD sameAs
    assert socials["facebook"] == "https://facebook.com/sandneselektriske"             # anchor; sharer rejected
    assert "x" not in socials                                                           # intent URL rejected
    jobs = by_field(res, "job_posting")
    assert len(jobs) == 1 and jobs[0]["availability"] == "available"
    assert jobs[0]["value"] == {"title": "Elektriker med fagbrev", "url": ORIGIN + "/karriere/elektriker/",
                                "date_posted": "2026-08-20", "valid_through": "2026-10-01", "location": "Sandnes, NO",
                                "source": "company_site"}
    news = {c["value"]["url"]: c["value"]["date"] for c in by_field(res, "news_item")}
    assert news[ORIGIN + "/nyheter/nye-lokaler/"] == "2026-09-05"          # RSS pubDate
    assert news[ORIGIN + "/nyheter/ny-kontrakt/"] == "2026-08-12"          # dd.mm.yyyy in anchor text
    assert news[ORIGIN + "/nyheter/2026/07/03/sommer/"] == "2026-07-03"    # date in URL
    assert ORIGIN + "/nyheter/udatert/" not in news and ORIGIN + "/nyheter/uten-dato/" not in news
    leaders = {c["value"]["name"]: c["value"]["title"] for c in by_field(res, "site_leader")}
    assert leaders == {"Ola Nordmann": "Daglig leder", "Kari Hansen": "Prosjektleder", "Per Olsen": "Styreleder"}
    assert by_field(res, "sitemap_lastmod")[0]["value"] == "2026-09-05"
    assert [c["value"] for c in by_field(res, "contact_email")] == ["post@sandneselektriske.no"]
    assert [c["value"] for c in by_field(res, "contact_phone")] == ["+4751123456"]
    assert by_field(res, "site_location")[0]["value"]["address"].startswith("Storgata 12, 4306")
    assert {p["kind"] for p in res["pages"]} >= {"homepage", "sitemap", "team", "careers", "news", "rss"}
    assert not [c for c in res["claims"] if c["availability"] == "not_available"]
    assert len(s.calls) <= 22


def test_every_available_claim_has_literal_evidence(crawled):
    res, _ = crawled
    ev = {e["id"]: e for e in res["evidence"]}
    ids = [c["id"] for c in res["claims"]]
    assert len(ids) == len(set(ids)) and all(i.startswith("site-") for i in ids)
    for c in res["claims"]:
        assert c["section"] in ("identity", "accounts", "leadership", "workplaces", "web", "hiring", "activity")
        if c["availability"] != "available":
            continue
        assert c["evidence_ids"], c
        for eid in c["evidence_ids"]:
            e = ev[eid]
            assert eid.startswith("evs-") and e["source_class"] == "company_owned"
            assert e["claim_span"].strip() and len(e["claim_span"]) <= 300, c


def test_not_available_claims_when_nothing_found():
    s = FakeSession({ORIGIN + "/": fx("empty.html")})
    home = page(ORIGIN + "/", fx("empty.html"))
    res = site.crawl(PROFILE, s, home, identity.assess(PROFILE, home), page_budget=4)
    na = {c["field"]: c for c in res["claims"] if c["availability"] == "not_available"}
    assert {"social_profile", "contact_email", "contact_phone", "site_leader", "news_item", "job_posting"} <= set(na)
    assert all(c["value"] is None and c["note"] and not c["evidence_ids"] for c in na.values())
    assert by_field(res, "official_website")[0]["availability"] == "available"


def test_careers_page_without_postings():
    home_html = "<html><head><title>Sandnes Elektriske AS</title></head><body><a href='/jobb/'>Jobb hos oss</a></body></html>"
    jobb = ("<html><head><title>Jobb</title></head><body><h1>Ledige stillinger</h1>"
            "<p>Vi har for tiden ingen ledige stillinger.</p><a href='/'>Hjem</a></body></html>")
    s = FakeSession({ORIGIN + "/": home_html, ORIGIN + "/jobb/": jobb})
    home = page(ORIGIN + "/", home_html)
    res = site.crawl(PROFILE, s, home, identity.assess(PROFILE, home), page_budget=3)
    cp = by_field(res, "careers_page")
    assert cp and cp[0]["section"] == "hiring"
    assert cp[0]["value"] == {"url": ORIGIN + "/jobb/", "hiring_cue": "ledige stillinger"}
    assert cp[0]["availability"] == "available" and "not machine-readable" in cp[0]["note"]
    assert by_field(res, "job_posting")[0]["availability"] == "not_available"


def test_budget_exhaustion_is_clean():
    s = FakeSession(SITE, cap=2)
    home = page(ORIGIN + "/", fx("home.html"))
    res = site.crawl(PROFILE, s, home, identity.assess(PROFILE, home), page_budget=6)
    assert len(s.calls) <= 2
    assert any("budget" in e["message"] for e in res["errors"])
    assert by_field(res, "official_website") and all(e["stage"] == "site" for e in res["errors"])
