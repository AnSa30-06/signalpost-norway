"""Website candidates for one organisation: registry hjemmeside, domain guesses, optional Brave search.

Candidates are ordered and deduplicated by registered domain. Nothing here is evidence; the identity
gate decides which candidate (if any) is the company's site.
"""
from __future__ import annotations

import os
import re
import urllib.parse

import tldextract

from .identity import name_tokens

BLOCKLIST = ("proff.no", "1881.no", "gulesider.no", "purehelp.no", "brreg.no", "regnskapstall.no", "linkedin.com",
             "facebook.com", "instagram.com", "youtube.com", "x.com", "twitter.com", "tiktok.com", "wikipedia.org",
             "finn.no", "nav.no", "allabolag", "northdata", "bizzy", "enin", "forvalt.no", "kompass", "cylex", "yelp",
             "trustpilot", "google.com")
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


def _brave(profile: dict, session) -> list[dict]:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    q = " ".join(s for s in (profile.get("name"), profile.get("municipality")) if s)
    url = BRAVE_URL + "?" + urllib.parse.urlencode({"q": q, "count": 5, "country": "NO"})
    try:
        r = session.get(url, company=str(profile.get("organisation_number")), kind="json", robots=False,
                        headers={"X-Subscription-Token": key, "Accept": "application/json"})
        data = r.json() or {}
    except Exception:
        return []
    out = []
    for hit in (data.get("web") or {}).get("results") or []:
        u = normalise_url(hit.get("url") if isinstance(hit, dict) else None)
        if u and not blocked_host(u):
            out.append({"url": u, "origin": "brave", "note": "web search result; transient, never evidence"})
    return out


def candidates(profile: dict, session) -> list[dict]:
    found: list[dict] = []
    reg = profile.get("registry") or {}
    home = normalise_url(reg.get("hjemmeside") or profile.get("hjemmeside") or "")
    if home and not blocked_host(home):
        found.append({"url": home, "origin": "registry", "note": "hjemmeside field in Enhetsregisteret"})
    for u in domain_guesses(profile.get("name") or ""):
        found.append({"url": u, "origin": "domain_guess", "note": "derived from legal name"})
    found.extend(_brave(profile, session))
    seen, out = set(), []
    for c in found:
        key = registered_domain(c["url"])
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out
