"""Website candidates for one organisation: registry hjemmeside, registry e-mail domain, domain guesses, Brave search.

Candidates are ordered and deduplicated by registered domain. Nothing here is evidence; the identity
gate decides which candidate (if any) is the company's site.
"""
from __future__ import annotations

import os
import re
import threading
import urllib.parse
from typing import Optional

import tldextract

from .identity import fold, name_tokens, tokens

BLOCKLIST = ("proff.no", "1881.no", "gulesider.no", "purehelp.no", "brreg.no", "regnskapstall.no", "linkedin.com",
             "facebook.com", "instagram.com", "youtube.com", "x.com", "twitter.com", "tiktok.com", "wikipedia.org",
             "finn.no", "nav.no", "allabolag", "northdata", "bizzy", "enin", "forvalt.no", "kompass", "cylex", "yelp",
             "trustpilot", "google.com",
             # directories that reproduce the legal name and organisation number (Builderr starter kit's list)
             "firmalisten.no", "companywall.no", "firmadatabasen.no", "sokfirma.no", "yra.no", "nor47business.com",
             "opencorporates.com", "dnb.com", "infobel", "hitta", "bedriftsdatabasen", "regnskap.no", "kununu")
GENERIC_SLUG_WORDS = {"eiendom", "invest", "holding", "bygg", "transport", "service", "consulting", "gruppen",
                      "industri", "handel", "drift", "utvikling", "partner", "partners", "solutions", "capital"}
MAX_GUESSES = 4
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

_extract = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)


def registered_domain(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    return _extract(host).top_domain_under_public_suffix.lower()


def blocked_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    reg = registered_domain(url)
    return any((reg == b or host.endswith("." + b)) if "." in b else (b in host) for b in BLOCKLIST)


def normalise_url(raw: str) -> str | None:
    """'example.no/om' -> 'https://example.no/om'; lower-case host; drop query/fragment; keep path."""
    s = str(raw or "").strip().strip("\"'")
    if not s:
        return None
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s.lstrip("/")
    p = urllib.parse.urlsplit(s)
    host = (p.hostname or "").lower().rstrip(".")
    if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    return urllib.parse.urlunsplit((p.scheme.lower(), host, p.path or "/", "", ""))


def domain_guesses(name: str) -> list[str]:
    toks = name_tokens(name)
    if not toks or all(t in GENERIC_SLUG_WORDS for t in toks):
        return []
    slugs = ["".join(toks)]
    if len(toks) > 1:
        slugs.append("-".join(toks))
        first = toks[0]
        if len(first) >= 5 and first not in GENERIC_SLUG_WORDS:
            slugs.append(first)
    urls = [f"https://{slug}.{tld}/" for tld in ("no", "com") for slug in slugs]
    return urls[:MAX_GUESSES]


# ---- Brave Search API: candidate discovery only -------------------------------------------------------------
# Builderr's source policy lists "search APIs used to discover candidates" and says "search results generate
# candidates; they are not claim evidence". Brave's terms forbid storing results without a storage-rights plan,
# so a search response is held in memory only (Session.get(store=False)): no snapshot, no query text in the
# request log. A result URL is fetched again independently and must pass the same exact-entity gate as any
# other candidate before anything from it is published.
BRAVE_KEY_ENVS = ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY")   # the first is the name Builderr's starter kit uses
BRAVE_USD_PER_QUERY = float(os.environ.get("SIGNALPOST_BRAVE_USD_PER_QUERY") or 0.005)   # $5 per 1,000 (Search plan)
MAX_SEARCH_DOMAINS = 3
_search_lock = threading.Lock()
_search = {"queries": 0, "max_queries": int(os.environ.get("SIGNALPOST_SEARCH_MAX_QUERIES") or 2500)}


def brave_key() -> str:
    return next((os.environ[k].strip() for k in BRAVE_KEY_ENVS if (os.environ.get(k) or "").strip()), "")


def brave_enabled() -> bool:
    return bool(brave_key())


def search_stats() -> dict:
    with _search_lock:
        return {"provider": "brave_search_api" if brave_enabled() else None, "queries": _search["queries"],
                "max_queries": _search["max_queries"], "cost_usd": round(_search["queries"] * BRAVE_USD_PER_QUERY, 4)}


def set_search_limit(max_queries: int) -> None:
    with _search_lock:
        _search["max_queries"] = max(0, int(max_queries))


def _take_query() -> bool:
    with _search_lock:
        if _search["queries"] >= _search["max_queries"]:
            return False
        _search["queries"] += 1
        return True


def search_queries(profile: dict) -> list[str]:
    """The exact legal name in quotes with the organisation number first: it surfaces pages that print the number,
    which the gate verifies outright. Only if that returns no usable candidate, the name with its municipality."""
    name = " ".join(str(profile.get("name") or "").split())
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    muni = " ".join(str(profile.get("municipality") or "").split())
    if not name:
        return []
    qs = [f'"{name}" {org}'.strip()]
    if muni:
        qs.append(f'"{name}" {muni}')
    return qs


def _brave_query(q: str, org: str, session) -> tuple[list[str], bool]:
    """Result URLs for one query (transient), and whether a query was actually spent."""
    key = brave_key()
    if not key or not _take_query():
        return [], False
    url = BRAVE_URL + "?" + urllib.parse.urlencode({"q": q, "count": 10, "country": "no", "search_lang": "nb",
                                                    "safesearch": "moderate", "spellcheck": "0"})
    r = session.get(url, company=org, kind="json", robots=False, store=False,
                    headers={"X-Subscription-Token": key, "Accept": "application/json"})
    try:
        data = r.json() or {}
    except Exception:
        data = {}
    return [hit.get("url") for hit in (data.get("web") or {}).get("results") or [] if isinstance(hit, dict)], True


def brave_candidates(profile: dict, session, exclude: set[str] = frozenset()) -> tuple[list[dict], int]:
    """Up to three distinct non-directory domains from search, and the number of queries spent.

    Called only after the registry website, the registry e-mail domain and the domain guesses have all failed,
    so no query is spent on a company the free routes already answer.

    A result is kept only when its host carries a distinctive word of the legal name. A query with the
    organisation number in it also returns news articles, supplier pages and tender notices that print the
    number; the gate would read such a page's "org.nr 914 922 941" as proof and publish a stranger's site as the
    company's own. Builderr's starter kit applies the same host rule to search candidates. The municipality
    and generic words ("eiendom", "holding") do not count as distinctive."""
    org = str(profile.get("organisation_number") or "")
    muni = set(tokens(profile.get("municipality")))
    want = [t for t in name_tokens(profile.get("name")) if len(t) >= 4 and t not in GENERIC_SLUG_WORDS and t not in muni]
    spent = 0
    for q in search_queries(profile):
        urls, used = _brave_query(q, org, session)
        spent += int(used)
        if not used:
            break
        seen, out = set(exclude), []
        for raw in urls:
            u = normalise_url(raw)
            d = registered_domain(u) if u else ""
            if not u or not d or blocked_host(u) or d in seen:
                continue
            seen.add(d)
            if any(t in re.sub(r"[^a-z0-9]", "", fold(d)) for t in want):
                out.append({"url": u, "origin": "brave", "note": "search result; transient, never evidence"})
        if out:
            return out[:MAX_SEARCH_DOMAINS], spent
    return [], spent


CONSUMER_MAIL_DOMAINS = {
    "gmail.com", "gmail.no", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.no",
    "live.com", "msn.com", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "online.no", "start.no", "frisurf.no", "c2i.net", "broadpark.no", "getmail.no", "sf-nett.no", "hotmail.co.uk",
}


def email_domain_candidate(profile: dict) -> Optional[dict]:
    """The domain of the e-mail address the company itself filed with the registry.

    Enhetsregisteret's ``epostadresse`` is stated by the company, so its domain is an official-source pointer at a
    domain the company uses. It is a candidate, not proof: 153 of 1,000 sampled companies filed an address on a
    company-owned domain, and among them were an accountant's domain, a parent's domain and an ISP's. The
    exact-entity gate still decides; this only puts the right door in front of it.
    """
    reg = profile.get("registry") or {}
    email = str(reg.get("epostadresse") or profile.get("epostadresse") or "").strip().lower()
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip(" .<>")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", domain) or domain in CONSUMER_MAIL_DOMAINS:
        return None
    url = normalise_url("https://" + domain)
    if not url or blocked_host(url):
        return None
    return {"url": url, "origin": "registry_email", "note": f"domain of the e-mail address filed with Enhetsregisteret ({email})"}


def candidates(profile: dict, session) -> list[dict]:
    found: list[dict] = []
    reg = profile.get("registry") or {}
    home = normalise_url(reg.get("hjemmeside") or profile.get("hjemmeside") or "")
    if home and not blocked_host(home):
        found.append({"url": home, "origin": "registry", "note": "hjemmeside field in Enhetsregisteret"})
    mail = email_domain_candidate(profile)
    if mail:
        found.append(mail)
    for u in domain_guesses(profile.get("name") or ""):
        found.append({"url": u, "origin": "domain_guess", "note": "derived from legal name"})
    seen, out = set(), []
    for c in found:
        key = registered_domain(c["url"])
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out
