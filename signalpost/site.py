"""Crawl a verified company website into claims + evidence.

Entry point: crawl(profile, session, homepage, identity, page_budget). Homepage is an already-fetched,
identity-verified FetchResult. Budget: `page_budget` targeted HTML pages, plus at most two sitemap requests
and one feed request, every one of them gated on session.remaining(org). Never raises.
"""
from __future__ import annotations

import email.utils
import re
import urllib.parse
from datetime import date
from typing import Optional

import extruct
from bs4 import BeautifulSoup

from .discovery import registered_domain
from .identity import fold, html_text, jsonld_nodes, node_types, span_around
from .models import (AVAILABLE, FAILED, NOT_AVAILABLE, IdGen, new_claim, new_error, new_evidence,
                     state_from_fetch)

SOURCE = "company_owned"
SOCIAL_HOSTS = {"linkedin.com": "linkedin", "facebook.com": "facebook", "instagram.com": "instagram",
                "youtube.com": "youtube", "x.com": "x", "twitter.com": "x", "tiktok.com": "tiktok"}
CANONICAL_HOST = {"linkedin": "linkedin.com", "facebook": "facebook.com", "instagram": "instagram.com",
                  "youtube": "youtube.com", "x": "x.com", "tiktok": "tiktok.com"}
PAGE_KINDS = [
    ("about", ("om-oss", "om oss", "about", "om-selskapet", "selskapet", "hvem-er-vi")),
    ("contact", ("kontakt", "contact")),
    ("team", ("team", "ansatte", "ledelse", "management", "people", "medarbeidere", "styret", "leadership")),
    ("careers", ("karriere", "career", "careers", "jobb", "ledige-stillinger", "jobs", "stillinger", "join-us")),
    ("news", ("nyheter", "news", "aktuelt", "blogg", "blog", "presse", "press", "media", "investor", "ir")),
]
KIND_RANK = {k: i for i, (k, _) in enumerate(PAGE_KINDS)}
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".doc", ".docx", ".xls", ".xlsx",
            ".xml", ".css", ".js", ".mp4", ".mp3", ".ics")
FREEMAIL = ("gmail.", "hotmail.", "outlook.", "live.", "yahoo.", "icloud.", "online.no", "getmail.")
NOT_AVAILABLE_NOTES = {
    ("web", "social_profile"): "no social profile links found on crawled pages",
    ("web", "contact_email"): "no contact email found on crawled pages",
    ("web", "contact_phone"): "no phone number found on crawled pages",
    ("leadership", "site_leader"): "no name/title pairs found on crawled pages",
    ("activity", "news_item"): "no dated news items found (feed, JSON-LD or news page)",
    ("hiring", "job_posting"): "no machine-readable job postings found on the site",
}
TITLE_RE = re.compile(r"(daglig leder|administrerende|\bCEO\b|\bCFO\b|\bCTO\b|\bCOO\b|\bCMO\b|styreleder|leder\b|"
                      r"sjef\b|direkt[øo]r\b|\bmanager\b|\bdirector\b|\bpartner\b|\bfounder\b|gr[üu]nder|"
                      r"\bhead of\b|\bchief\b)", re.I)
NAME_RE = re.compile(r"^[A-ZÆØÅ][a-zæøåé]+(?:[ \-][A-ZÆØÅ][a-zæøåé]+){1,2}$")
NAME_TITLE_RE = re.compile(r"^([A-ZÆØÅ][a-zæøåé]+(?:[ \-][A-ZÆØÅ][a-zæøåé]+){1,2})\s*[,–\-|:/]\s*(.{2,80})$")
HIRING_CUES = ("ledige stillinger", "open positions", "vacancies", "vi soker", "we are hiring", "join our team")
PHONE_RE = re.compile(r"(?<![\d+])((?:\+47|0047)?[\s.\-]?(?:[2-9]\d(?:[\s.]?\d{2}){3}|[2-9]\d{2}[\s.]?\d{2}[\s.]?\d{3}))(?!\d)")
PHONE_CUE = re.compile(r"(tlf|telefon|phone|mobil|ring oss|tel)\W{0,6}$", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
DATE_URL_RE = re.compile(r"/(\d{4})/(\d{2})(?:/(\d{2}))?/")
MAX_SITEMAP_LOCS = 500


# ---- small pure helpers -----------------------------------------------------------------------------------
def normalise_social(url: str) -> Optional[dict]:
    """Canonical {"platform","url"} for a profile link; None for share/intent/plugin/non-profile URLs."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except (ValueError, AttributeError):
        return None
    host = (p.hostname or "").lower().removeprefix("www.")
    plat = next((v for k, v in SOCIAL_HOSTS.items() if host == k or host.endswith("." + k)), None)
    parts = [x for x in p.path.split("/") if x]
    if not plat or not parts:
        return None
    low = [x.lower() for x in parts]
    if plat == "linkedin":
        if low[0] not in ("company", "showcase") or len(parts) < 2:
            return None
        parts = parts[:2]
    elif plat == "facebook":
        if low[0] in ("sharer", "sharer.php", "share.php", "share", "dialog", "plugins", "login", "login.php",
                      "profile.php", "events", "groups", "hashtag", "photo.php", "watch", "policy.php", "privacy"):
            return None
        parts = parts[:3] if low[0] == "pages" else parts[:1]
    elif plat == "instagram":
        if low[0] in ("p", "reel", "reels", "stories", "explore", "accounts", "share", "tv"):
            return None
        parts = parts[:1]
    elif plat == "youtube":
        if parts[0].startswith("@"):
            parts = parts[:1]
        elif low[0] in ("channel", "c", "user") and len(parts) >= 2:
            parts = parts[:2]
        else:
            return None
    elif plat == "x":
        if low[0] in ("intent", "share", "home", "search", "i", "hashtag", "login", "explore", "settings"):
            return None
        parts = parts[:1]
    elif plat == "tiktok":
        if not parts[0].startswith("@"):
            return None
        parts = parts[:1]
    return {"platform": plat, "url": f"https://{CANONICAL_HOST[plat]}/{'/'.join(parts)}"}


def normalise_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0047"):
        digits = digits[4:]
    elif len(digits) == 10 and digits.startswith("47"):
        digits = digits[2:]
    return "+47" + digits if len(digits) == 8 and digits[0] in "23456789" else None


def iso_date(text: str) -> Optional[str]:
    """'2026-09-05T10:00:00Z', '05.09.2026', RFC 2822 -> '2026-09-05'; None when unparseable."""
    s = (text or "").strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", s)
        if m:
            d, mo, y = m.groups()
        else:
            try:
                return email.utils.parsedate_to_datetime(s).date().isoformat()
            except (TypeError, ValueError, IndexError):
                return None
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def literal_span(text: str, needle: str, width: int = 120) -> str:
    """Like span_around but empty when `needle` is not literally in `text`."""
    return span_around(text, needle, width) if needle and needle.lower() in (text or "").lower() else ""


def classify(url: str, anchor_text: str = "") -> Optional[str]:
    """Page kind from path keywords (anchor text only as fallback)."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    for hay in (" " + re.sub(r"[-_/.+]", " ", fold(path)) + " ", " " + fold(anchor_text) + " "):
        for kind, words in PAGE_KINDS:
            for w in words:
                if re.search(r"\b" + re.escape(w.replace("-", " ")) + r"\b", hay):
                    return kind
    return None


def _clean_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", "", ""))


def _addr_text(addr) -> str:
    if isinstance(addr, str):
        return addr.strip()
    if isinstance(addr, dict):
        keys = ("streetAddress", "postalCode", "addressLocality", "addressCountry")
        return ", ".join(str(addr[k]).strip() for k in keys if addr.get(k))
    return ""


def _md_convert(it):
    """extruct microdata {"type","properties"} -> JSON-LD-shaped {"@type", ...}, recursively."""
    if isinstance(it, dict) and it.get("type") and isinstance(it.get("properties"), dict):
        return {"@type": str(it["type"]).rsplit("/", 1)[-1], **{k: _md_convert(v) for k, v in it["properties"].items()}}
    if isinstance(it, list):
        return [_md_convert(v) for v in it]
    return it


def _microdata_nodes(items) -> list[dict]:
    return jsonld_nodes([_md_convert(i) for i in items or []])


# ---- the crawl --------------------------------------------------------------------------------------------
class _Crawl:
    def __init__(self, profile, session, homepage, identity, page_budget):
        self.org = str(profile.get("organisation_number") or "")
        self.profile, self.session, self.home, self.identity = profile, session, homepage, identity or {}
        self.budget = max(0, int(page_budget or 0))
        self.ids = IdGen()
        self.claims: list[dict] = []
        self.evidence: list[dict] = []
        self.pages: list[dict] = []
        self.errors: list[dict] = []
        p = urllib.parse.urlsplit(homepage.final_url or homepage.url)
        self.origin = f"{p.scheme}://{p.netloc}"
        self.domain = registered_domain(self.origin)
        self.exhausted = False
        self.brand = self.identity.get("public_brand")
        # collected before emission: key -> (value, page, span, method, confidence)
        self.socials: dict[str, tuple] = {}
        self.emails: dict[str, tuple] = {}
        self.phones: dict[str, tuple] = {}
        self.leaders: dict[tuple, tuple] = {}
        self.locations: dict[str, tuple] = {}
        self.jobs: dict[str, tuple] = {}
        self.news: dict[str, tuple] = {}
        self.links: dict[str, tuple] = {}          # clean url -> (kind, anchor text)
        self.feeds: list[str] = []
        self.careers_pages: list[tuple] = []       # (page, soup, text)
        self.job_pages: set[str] = set()

    # -- output helpers --
    def claim(self, section, field, value, page, span, method, confidence=1.0, note=None, effective_date=None):
        span = span or (value if isinstance(value, str) else str(value))
        ev = new_evidence(self.ids, page, SOURCE, span, method, prefix="evs")
        self.evidence.append(ev)
        self.claims.append(new_claim(self.ids, section, field, value, AVAILABLE, [ev["id"]], confidence, note,
                                     effective_date=effective_date, prefix="site"))

    def not_available(self, section, field, note):
        self.claims.append(new_claim(self.ids, section, field, None, NOT_AVAILABLE, [], 1.0, note, prefix="site"))

    def error(self, message, availability, url=None):
        self.errors.append(new_error("site", message, availability, url))

    def fetch(self, url, kind, label):
        if self.exhausted:
            return None
        if self.session.remaining(self.org) <= 0:
            self.exhausted = True
            self.error("request budget exhausted before " + url, NOT_AVAILABLE, url)
            return None
        try:
            r = self.session.get(url, company=self.org, kind=kind)
        except Exception as exc:
            self.error(f"fetch failed: {type(exc).__name__}: {exc}", FAILED, url)
            return None
        self.pages.append({"url": r.final_url or url, "kind": label, "status": r.status})
        if r.budget_exhausted:
            self.exhausted = True
        if not r.ok:
            self.error(r.error or f"http_{r.status}", state_from_fetch(r), url)
            return None
        return r

    # -- main flow --
    def run(self):
        home = self.home
        self.pages.append({"url": home.final_url or home.url, "kind": "homepage", "status": home.status})
        soup = BeautifulSoup(home.text or "", "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        self.claim("web", "official_website", self.origin + "/", home, self.identity.get("claim_span") or title,
                   "identity_gate", confidence=float(self.identity.get("score") or 0.0))
        if self.brand:
            site_name = next((m.get("content") for m in soup.find_all("meta")
                              if (m.get("property") or "").lower() == "og:site_name" and m.get("content")), "")
            self.claim("identity", "public_brand", self.brand, home, site_name or title or self.brand, "og_or_title")
        if title:
            self.claim("web", "website_title", title[:300], home, title, "html_title")
        desc = ""
        for m in soup.find_all("meta"):
            key = (m.get("name") or m.get("property") or "").lower()
            if key in ("description", "og:description") and (m.get("content") or "").strip():
                desc = m["content"].strip()
                if key == "description":
                    break
        if desc:
            self.claim("web", "website_description", desc[:300], home, desc, "meta_description")
        self.safe(self.process_html, home, "homepage")
        self.safe(self.sitemap)
        self.safe(self.targeted_pages)
        self.safe(self.feed)
        self.safe(self.careers_fallback)
        self.emit()

    def safe(self, fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # per-stage isolation; never raise out of crawl()
            self.error(f"{fn.__name__} failed: {type(exc).__name__}: {str(exc)[:150]}", FAILED)

    # -- per page --
    def process_html(self, page, kind):
        raw = page.text or ""
        soup = BeautifulSoup(raw, "lxml")
        for t in soup(["script", "style", "noscript", "template"]):
            t.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        base = page.final_url or page.url
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            low = href.lower()
            atext = a.get_text(" ", strip=True)
            if low.startswith("mailto:"):
                self.add_email(href[7:].split("?")[0].strip(), page, href)
                continue
            if low.startswith("tel:"):
                self.add_phone(href[4:], page, href, 1.0)
                continue
            if low.startswith(("javascript:", "#")):
                continue
            absu = urllib.parse.urljoin(base, href)
            if urllib.parse.urlsplit(absu).scheme not in ("http", "https"):
                continue
            soc = normalise_social(absu)
            if soc:
                self.socials.setdefault(soc["platform"], (soc, page, href, "anchor_href", 1.0))
                continue
            if registered_domain(absu) == self.domain and not absu.lower().split("?")[0].endswith(SKIP_EXT):
                cu = _clean_url(absu)
                if cu.rstrip("/") != self.origin and cu not in self.links:
                    self.links[cu] = (classify(cu, atext), atext)
            if kind == "news":
                self.dated_anchor(a, absu, atext, page, text)
        for e in EMAIL_RE.findall(text):
            if registered_domain("https://" + e.split("@")[1]) == self.domain:
                self.add_email(e, page, e)
        for m in PHONE_RE.finditer(text):
            raw_num = m.group(1)
            if raw_num.lstrip().startswith(("+47", "0047")) or PHONE_CUE.search(text[max(0, m.start() - 14):m.start()]):
                self.add_phone(raw_num, page, span_around(text, raw_num, 60), 0.8)
        if kind in ("homepage", "news"):
            for link in soup.find_all("link", rel=True, href=True):
                if "alternate" in [r.lower() for r in link.get("rel")] and re.search(r"(rss|atom)\+xml", link.get("type") or "", re.I):
                    self.feeds.append(urllib.parse.urljoin(base, link["href"]))
        self.structured(page, raw)
        if kind == "team":
            self.team_text(page, soup)
        if kind == "careers":
            self.careers_pages.append((page, soup, text))

    def add_email(self, addr, page, span):
        addr = addr.strip().lower().rstrip(".")
        if not EMAIL_RE.fullmatch(addr) or any(f in addr.split("@")[1] for f in FREEMAIL):
            return
        self.emails.setdefault(addr, (addr, page, span, "mailto_or_text", 1.0))

    def add_phone(self, raw, page, span, confidence):
        norm = normalise_phone(raw)
        if norm:
            self.phones.setdefault(norm, (norm, page, span, "tel_or_text", confidence))

    def structured(self, page, raw):
        try:
            md = extruct.extract(raw, base_url=page.final_url or page.url, syntaxes=["json-ld", "microdata", "opengraph"],
                                 errors="ignore")
        except Exception as exc:
            self.error(f"extruct failed: {type(exc).__name__}", FAILED, page.final_url)
            return
        nodes = jsonld_nodes(md.get("json-ld") or []) + _microdata_nodes(md.get("microdata") or [])
        for node in nodes:
            types = node_types(node)
            sp = lambda needle, w=160: span_around(raw, str(needle), w)  # noqa: E731
            if types & {"Organization", "LocalBusiness", "Corporation"}:
                same = node.get("sameAs") or []
                for u in (same if isinstance(same, list) else [same]):
                    soc = normalise_social(str(u)) if isinstance(u, str) else None
                    if soc:
                        self.socials.setdefault(soc["platform"], (soc, page, sp(u), "jsonld_sameAs", 1.0))
                if isinstance(node.get("telephone"), str):
                    self.add_phone(node["telephone"], page, sp(node["telephone"]), 1.0)
                if isinstance(node.get("email"), str):
                    self.add_email(node["email"].replace("mailto:", ""), page, sp(node["email"]))
                addr = _addr_text(node.get("address"))
                if addr:
                    name = node.get("name") if isinstance(node.get("name"), str) else self.brand
                    self.locations.setdefault(addr, ({"name": name or self.brand, "address": addr[:200]}, page,
                                                     sp(addr.split(",")[0]), "jsonld_address", 1.0))
            if "Person" in types and isinstance(node.get("name"), str) and isinstance(node.get("jobTitle"), str):
                self.add_leader(node["name"], node["jobTitle"], page, sp(node["name"]), "jsonld_person", 1.0)
            if "JobPosting" in types and isinstance(node.get("title"), str):
                loc = node.get("jobLocation")
                loc = loc[0] if isinstance(loc, list) and loc else loc
                loc_txt = _addr_text(loc.get("address")) if isinstance(loc, dict) else _addr_text(loc)
                url = node.get("url") if isinstance(node.get("url"), str) else (page.final_url or page.url)
                value = {"title": node["title"].strip()[:200], "url": url, "date_posted": iso_date(str(node.get("datePosted") or "")),
                         "valid_through": iso_date(str(node.get("validThrough") or "")), "location": loc_txt or None,
                         "source": "company_site"}
                self.jobs.setdefault(url + "#" + value["title"], (value, page, sp(node["title"]), "jsonld_jobposting", 1.0))
                self.job_pages.add(page.final_url or page.url)
            if types & {"NewsArticle", "BlogPosting", "Article"}:
                title = node.get("headline") or node.get("name")
                url = node.get("url") or (node.get("mainEntityOfPage") if isinstance(node.get("mainEntityOfPage"), str) else None)
                d = iso_date(str(node.get("datePublished") or ""))
                if isinstance(title, str) and isinstance(url, str) and d:
                    self.news.setdefault(url, ({"title": title.strip()[:200], "url": url, "date": d}, page, sp(title),
                                               "jsonld_article", 1.0))

    def add_leader(self, name, title, page, span, method, confidence):
        name, title = name.strip(), re.sub(r"\s+", " ", title).strip(" ,.-–|:")
        if len(self.leaders) < 15 and name and title:
            self.leaders.setdefault((name.lower(), title.lower()), ({"name": name, "title": title[:80]}, page, span, method, confidence))

    def team_text(self, page, soup):
        strings = [s.strip() for s in soup.stripped_strings if s.strip()]
        for i, s in enumerate(strings):
            m = NAME_TITLE_RE.match(s)
            if m and NAME_RE.match(m.group(1)) and TITLE_RE.search(m.group(2)):
                self.add_leader(m.group(1), m.group(2), page, s, "team_page_text", 0.8)
            elif NAME_RE.match(s) and i + 1 < len(strings) and len(strings[i + 1]) <= 80 and TITLE_RE.search(strings[i + 1]):
                self.add_leader(s, strings[i + 1], page, s + " " + strings[i + 1], "team_page_text", 0.8)

    def dated_anchor(self, a, absu, atext, page, page_text):
        if registered_domain(absu) != self.domain or len(atext) < 8 or absu in self.news or len(self.news) >= 10:
            return
        d = iso_date(atext)
        t = a.find("time")
        if not d and t is not None:
            d = iso_date(t.get("datetime") or t.get_text())
        if not d:
            m = DATE_URL_RE.search(urllib.parse.urlsplit(absu).path)
            if m and m.group(3):
                d = iso_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
            elif m:
                d = f"{m.group(1)}-{m.group(2)}"
        if d:
            self.news.setdefault(absu, ({"title": atext[:200], "url": absu, "date": d}, page,
                                        span_around(page_text, atext[:80], 160), "news_page_anchor", 0.8))

    # -- sitemap / targeted / feed / careers --
    def sitemap(self):
        urls = list(self.session.sitemaps_from_robots(self.origin) or []) or [self.origin + "/sitemap.xml"]
        r = self.fetch(urls[0], "xml", "sitemap")
        if not r:
            return
        xs = BeautifulSoup(r.text, "xml")
        if xs.find("sitemapindex"):
            child = next((l.get_text(strip=True) for l in xs.find_all("loc")
                          if not re.search(r"image|video", l.get_text(), re.I)), None)
            r = self.fetch(child, "xml", "sitemap") if child else None
            if not r:
                return
            xs = BeautifulSoup(r.text, "xml")
        best = None
        for u in xs.find_all("url")[:MAX_SITEMAP_LOCS]:
            loc, lm = u.find("loc"), u.find("lastmod")
            loc_txt = loc.get_text(strip=True) if loc else ""
            if loc_txt and registered_domain(loc_txt) == self.domain and not loc_txt.lower().endswith(SKIP_EXT):
                cu = _clean_url(loc_txt)
                if cu.rstrip("/") != self.origin:
                    self.links.setdefault(cu, (classify(cu), ""))
            d = iso_date(lm.get_text()) if lm else None
            if d and (best is None or d > best[0]):
                best = (d, re.sub(r"\s+", " ", str(u)))
        if best:
            self.claim("activity", "sitemap_lastmod", best[0], r, best[1], "sitemap_lastmod")

    def targeted_pages(self):
        cands = [(u, k, t) for u, (k, t) in self.links.items() if k]
        cands.sort(key=lambda c: (KIND_RANK[c[1]], c[0].count("/"), len(c[0])))
        first = {}
        for c in cands:
            first.setdefault(c[1], c)
        ordered = list(first.values()) + [c for c in cands if c not in first.values()]
        fetched = 0
        for url, kind, _ in ordered:
            if fetched >= self.budget or self.exhausted:
                break
            r = self.fetch(url, "html", kind)
            fetched += 1
            if not r:
                continue
            if registered_domain(r.final_url or url) != self.domain:
                self.error("redirected off registered domain", NOT_AVAILABLE, url)
                continue
            if (r.final_url or url).rstrip("/") == (self.home.final_url or "").rstrip("/"):
                continue
            self.safe(self.process_html, r, kind)

    def feed(self):
        if not self.feeds:
            return
        r = self.fetch(self.feeds[0], "xml", "rss")
        if not r:
            return
        xs = BeautifulSoup(r.text, "xml")
        for it in (xs.find_all("item") or xs.find_all("entry"))[:25]:
            title, link = it.find("title"), it.find("link")
            url = (link.get("href") or link.get_text(strip=True)) if link else ""
            d = None
            for tag in ("pubDate", "published", "updated", "dc:date"):
                el = it.find(tag)
                d = iso_date(el.get_text()) if el else None
                if d:
                    break
            if title and url and d and url not in self.news:
                ttl = title.get_text(strip=True)[:200]
                self.news[url] = ({"title": ttl, "url": url, "date": d}, r, span_around(r.text, ttl, 160), "rss_item", 1.0)

    def careers_fallback(self):
        """A careers page without JSON-LD JobPosting yields one `careers_page` claim, never guessed postings.
        Anchor text on such pages ("Prosjekter", "Lærling", article teasers) is not a job ad; publishing it would
        cost exact-claim precision. Individual ads come from JSON-LD or the NAV connector instead."""
        for page, soup, text in self.careers_pages:
            purl = page.final_url or page.url
            if purl in self.job_pages:
                continue
            folded = fold(text)
            cue = next((c for c in HIRING_CUES if c in folded), "")
            self.claim("hiring", "careers_page", {"url": purl, "hiring_cue": cue or None}, page, literal_span(text, cue) or text[:200],
                       "careers_page", 0.9, note="careers page exists; individual postings not machine-readable on the site")

    # -- emission --
    def emit(self):
        for plat in sorted(self.socials):
            value, page, span, method, conf = self.socials[plat]
            self.claim("web", "social_profile", value, page, span, method, conf)
        emails = sorted(self.emails.values(), key=lambda e: (registered_domain("https://" + e[0].split("@")[1]) != self.domain, e[0]))
        for value, page, span, method, conf in emails[:3]:
            self.claim("web", "contact_email", value, page, span, method, conf)
        for value, page, span, method, conf in list(self.phones.values())[:3]:
            self.claim("web", "contact_phone", value, page, span, method, conf)
        for value, page, span, method, conf in list(self.leaders.values())[:15]:
            self.claim("leadership", "site_leader", value, page, span, method, conf)
        for value, page, span, method, conf in list(self.locations.values())[:5]:
            self.claim("workplaces", "site_location", value, page, span, method, conf)
        for value, page, span, method, conf in list(self.jobs.values())[:25]:
            self.claim("hiring", "job_posting", value, page, span, method, conf, effective_date=value.get("date_posted"))
        newest = sorted(self.news.values(), key=lambda n: n[0]["date"], reverse=True)[:10]
        for value, page, span, method, conf in newest:
            self.claim("activity", "news_item", value, page, span, method, conf, effective_date=value["date"])
        have = {(c["section"], c["field"]) for c in self.claims}
        for key, note in NOT_AVAILABLE_NOTES.items():
            if key not in have:
                self.not_available(key[0], key[1], note)


def crawl(profile: dict, session, homepage, identity: dict, page_budget: int) -> dict:
    c = _Crawl(profile, session, homepage, identity, page_budget)
    try:
        c.run()
    except Exception as exc:  # last line of defence: the contract is "never raise"
        c.error(f"crawl failed: {type(exc).__name__}: {str(exc)[:150]}", FAILED, getattr(homepage, "final_url", None))
    return {"claims": c.claims, "evidence": c.evidence, "pages": c.pages, "errors": c.errors}
