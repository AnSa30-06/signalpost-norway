"""Exact-entity gate: does a fetched homepage belong to THIS organisation?

Deterministic: org number on page -> 1.0; every legal-name token in the identity text plus one registry
corroborator -> 0.95; tokens only -> 0.8; parked page -> 0.1; anything else -> 0.3.
Also exports the folding / tokenising / JSON-LD helpers shared by discovery.py and site.py.
"""
from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from typing import Any, Iterable, Optional

from bs4 import BeautifulSoup

LEGAL_FORMS = {"as", "asa", "ans", "da", "enk", "nuf", "sa", "ba", "ks", "sti", "brl"}
GENERIC_TOKENS = {"og", "and", "the", "group", "gruppen", "norge", "norway", "as"}
ORG_TYPES = {"Organization", "LocalBusiness", "Corporation"}
PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "buy this domain", "this domain is parked", "domain parking",
    "parked domain", "domenet er til salgs", "dette domenet er til salgs", "kjop dette domenet", "domene til salgs",
    "hugedomains", "sedo.com", "dan.com", "afternic", "godaddy.com/domains", "her flytter snart en ny gjest",
    "webhotell er ikke satt opp", "this webpage is parked", "parked by", "domeneparkering", "parkert domene",
    "domain has been registered", "domenet er registrert", "coming soon - this domain",
    # Hosting-provider placeholders. A One.com placeholder titled "Hosted By One.com | Webhosting made simple"
    # was published as the official website of PAULSEN DRIFT AS, on the strength of the domain name alone.
    "hosted by one.com", "webhosting made simple", "welcome to nginx", "apache2 ubuntu default page",
    "default web site page", "site not configured", "nettsiden er under konstruksjon", "under construction",
    "parkeringsside", "er parkert", "the domain name is parked", "is registered, but the owner",
)
NORWAY_MARKERS = ("norge", "noreg", "norway", "organisasjonsnummer", "org.nr", "orgnr", "org nr")
TITLE_SEPARATORS = (" | ", " – ", " — ", " - ", " · ", " :: ", " » ")
_FOLD = str.maketrans({"æ": "ae", "ø": "o", "å": "a", "Æ": "ae", "Ø": "o", "Å": "a"})


# ---- shared helpers ---------------------------------------------------------------------------------------
def fold(text: Any) -> str:
    """Lower-case ASCII folding: æøå -> ae/o/a, other accents stripped."""
    s = str(text or "").translate(_FOLD)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def tokens(text: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def name_tokens(name: Any) -> list[str]:
    """Legal name -> distinctive tokens (legal-form words and generic words removed, length >= 2)."""
    return [t for t in tokens(name) if t not in LEGAL_FORMS and t not in GENERIC_TOKENS and len(t) >= 2]


def html_text(html: str) -> str:
    """Visible text of an HTML document, whitespace collapsed."""
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def span_around(text: str, needle: str, width: int = 80) -> str:
    """~width chars of `text` around the first (case-insensitive) occurrence of `needle`; needle if absent."""
    if not needle:
        return ""
    i = (text or "").lower().find(needle.lower())
    if i < 0:
        return needle[:300]
    half = max(0, (width - len(needle)) // 2)
    return re.sub(r"\s+", " ", text[max(0, i - half): i + len(needle) + half]).strip()[:300]


def jsonld_nodes(data: Any) -> list[dict]:
    """Every dict carrying an @type inside a JSON-LD structure (handles @graph and nesting)."""
    out: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("@type"):
                out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def node_types(node: dict) -> set[str]:
    t = node.get("@type")
    items = t if isinstance(t, list) else [t]
    return {str(x).rsplit("/", 1)[-1].rsplit(":", 1)[-1] for x in items if x}


def jsonld_from_soup(soup: BeautifulSoup) -> list[Any]:
    blocks = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            blocks.append(json.loads(tag.string or tag.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return blocks


def _str_values(node: dict, keys: Iterable[str]) -> list[str]:
    vals = []
    for k in keys:
        v = node.get(k)
        for item in (v if isinstance(v, list) else [v]):
            if isinstance(item, str) and item.strip():
                vals.append(item.strip())
    return vals


def clean_title(title: str) -> list[str]:
    """Split a <title> on the usual separators, drop empties."""
    parts = [title or ""]
    for sep in TITLE_SEPARATORS:
        parts = [p for chunk in parts for p in chunk.split(sep)]
    return [p.strip() for p in parts if p.strip()]


# ---- the gate ---------------------------------------------------------------------------------------------
def _org_pattern(orgnr: str) -> Optional[re.Pattern]:
    digits = re.sub(r"\D", "", str(orgnr or ""))
    if len(digits) != 9:
        return None
    return re.compile(r"(?<!\d)" + r"[\s.]?".join(digits) + r"(?!\d)")


ORG_LABEL_RE = re.compile(
    r"(?:org(?:anisasjons)?\.?\s*(?:nr|nummer)\.?|foretaks(?:nr|nummer)|"
    r"company\s+(?:reg\.?|registration)\s*(?:no\.?|number)|vat\s*(?:no\.?|number))"
    r"[^0-9]{0,20}((?:NO)?\s*\d[\s.]?\d[\s.]?\d[\s.]?\d[\s.]?\d[\s.]?\d[\s.]?\d[\s.]?\d[\s.]?\d)", re.I)


def other_org_numbers(full_text: str, our_org: str) -> list[str]:
    """Nine-digit organisation numbers the page LABELS as its own, excluding ours.

    A site that states its own organisation number has named its legal entity. industrifinans.no carries the
    right brand and the exact registered street of INDUSTRIFINANS AS (924351020) and states
    "Org.nummer: 993 075 558" — a different entity at the same address. Name and address alone cannot separate
    a sister company from the company; a stated organisation number can.
    """
    ours = re.sub(r"\D", "", str(our_org or ""))
    found = []
    for m in ORG_LABEL_RE.finditer(full_text or ""):
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) == 9 and digits != ours and digits not in found:
            found.append(digits)
    return found


def _norway_signal(folded_text: str, full_text: str, hostname: str = "") -> bool:
    """Does anything tie this page to Norway?

    This guards the domain-spells-the-name corroborator against a foreign namesake: hoainvest.com spells
    HOA INVEST AS exactly and is a California investment firm.

    Every signal here must be unambiguous. A "4 digits then a capitalised word" test for a Norwegian postcode
    was tried and removed: it read the Californian street address "2300 Palm" as a Norwegian postcode and let
    that namesake through. A .no domain, a +47 number and the country's name cannot be produced by accident.
    """
    if fold(hostname).rstrip(".").endswith(".no"):
        return True
    if any(m in folded_text for m in NORWAY_MARKERS):
        return True
    if re.search(r"\+\s?47[\s\d]{6,}", full_text):                 # Norwegian dialling code
        return True
    if re.search(r"@[\w.-]+\.no\b", full_text) or re.search(r"https?://[\w.-]+\.no\b", full_text):
        return True
    return False


def _corroborators(profile: dict, folded_text: str) -> list[str]:
    reg = profile.get("registry") or {}
    hits = []
    in_name = set(name_tokens(profile.get("name")))  # "Sandnes" in "Sandnes Elektriske" corroborates nothing
    addresses = [reg.get("forretningsadresse") or {}, reg.get("postadresse") or {}]
    for addr in addresses:
        pc = str(addr.get("postnummer") or "").strip()
        if re.fullmatch(r"\d{4}", pc) and re.search(rf"(?<!\d){pc}(?!\d)", folded_text):
            hits.append(f"postcode:{pc}")
        for line in addr.get("adresse") or []:
            m = re.match(r"([^\d,]+)", fold(line))
            street = m.group(1).strip() if m else ""
            if len(street) >= 4 and re.search(rf"\b{re.escape(street)}\b", folded_text):
                hits.append(f"street:{street}")
        for place in (addr.get("poststed"), addr.get("kommune")):
            p = fold(place).strip()
            if len(p) >= 3 and p not in in_name and re.search(rf"\b{re.escape(p)}\b", folded_text):
                hits.append(f"place:{p}")
    muni = fold(profile.get("municipality")).strip()
    if len(muni) >= 3 and muni not in in_name and re.search(rf"\b{re.escape(muni)}\b", folded_text):
        hits.append(f"place:{muni}")
    return sorted(set(hits))


def assess(profile: dict, page, extra_text: str = "") -> dict:
    html = page.text or ""
    soup = BeautifulSoup(html, "lxml")
    text = html_text(html)
    full_text = (text + " " + (extra_text or "")).strip()
    hostname = (urllib.parse.urlsplit(page.final_url or page.url or "").hostname or "").lower()

    # identity parts, in order of trust
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = {}
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or "").lower()
        if key in ("og:site_name", "og:title") and m.get("content"):
            meta.setdefault(key, m["content"].strip())
    ld_names: list[str] = []
    for node in jsonld_nodes(jsonld_from_soup(soup)):
        if node_types(node) & ORG_TYPES:
            ld_names.extend(_str_values(node, ("name", "legalName", "alternateName")))
    footer = " ".join(t.get_text(" ", strip=True) for t in soup.find_all(["footer", "address"]))[:2000]
    parts = [("title", title), ("og:site_name", meta.get("og:site_name", "")), ("og:title", meta.get("og:title", "")),
             *[("json-ld", n) for n in ld_names], ("footer", footer), ("hostname", hostname)]

    brand_candidates = [meta.get("og:site_name"), *ld_names, *clean_title(title)]
    public_brand = _pick_brand(brand_candidates, profile.get("name"))
    aliases = []
    for a in [*ld_names, meta.get("og:site_name"), meta.get("og:title"), *clean_title(title)]:
        if a and a != public_brand and a not in aliases and len(a) <= 80:
            aliases.append(a)

    result = {"score": 0.3, "status": "related_or_uncertain", "reasons": [], "claim_span": "",
              "public_brand": public_brand, "aliases": aliases[:5]}

    pat = _org_pattern(profile.get("organisation_number"))
    m = pat.search(full_text) if pat else None
    if m:
        result.update(score=1.0, reasons=["org_number_on_page"], claim_span=span_around(full_text, m.group(0), 80))
        return _finish(result)

    folded_all = fold(full_text)
    marker = next((mk for mk in PARKED_MARKERS if mk in folded_all), None)
    if marker:
        result.update(score=0.1, reasons=["parked"], claim_span=span_around(full_text, marker, 80) or marker)
        return _finish(result)

    want = name_tokens(profile.get("name"))
    # The company name must appear in the page's OWN content. Matching it against the hostname proves only that
    # somebody registered that domain, and then counting the same domain again as corroboration counts one fact
    # twice. That circularity published a hosting placeholder and a Californian namesake as verified websites.
    content_parts = [(k, v) for k, v in parts if k != "hostname"]
    have_content = set()
    for _, part in content_parts:
        have_content.update(tokens(part))
    have_content.update(tokens(full_text))   # the page's own visible text is self-identification too
    host_compact = re.sub(r"[^a-z0-9]", "", fold(hostname))
    present = [t for t in want if t in have_content]
    present_or_host = [t for t in want if t in have_content or (len(t) >= 4 and t in host_compact)]
    best_part = max(content_parts, key=lambda p: (len(set(tokens(p[1])) & set(want)), -len(p[1] or "x")))[1] or title
    span = re.sub(r"\s+", " ", best_part).strip()[:300]
    if want and not set(want) & set(tokens(best_part)):
        # the name is in the body rather than the title: quote where it actually appears
        longest = max(want, key=len)
        span = span_around(full_text, longest, 110) or span
    if want and len(present) == len(want):
        corr = _corroborators(profile, folded_all)
        # A one-word name ("Semaphore", "Vitamat") plus a city name is not proof: cities appear on many pages.
        # Single-token names need a postcode or street match; multi-token names may use any corroborator.
        strong = [c for c in corr if c.startswith(("postcode:", "street:"))]
        # A domain that spells out a MULTI-WORD legal name ("afgruppen.no" for AF GRUPPEN ASA) is itself strong:
        # the company registered it. Single-word names are excluded, because "vit.no" or "skard.no" could be anyone.
        raw = [t for t in tokens(profile.get("name")) if t not in LEGAL_FORMS]
        name_compact = "".join(raw)
        labels = [l for l in fold(hostname).split(".") if l and l != "www"]
        if (len(raw) >= 2 and len(name_compact) >= 7 and labels
                and re.sub(r"[^a-z0-9]", "", labels[0]) == name_compact
                and _norway_signal(folded_all, full_text, hostname)):
            corr = sorted(set(corr) | {f"domain_is_legal_name:{labels[0]}"})
            strong = strong or [f"domain_is_legal_name:{labels[0]}"]
        # A different, self-declared organisation number means this page belongs to another legal entity.
        # Keep it as a candidate, never as a verified website.
        others = other_org_numbers(full_text, profile.get("organisation_number"))
        if others:
            result.update(score=0.8, reasons=["name_match_but_other_org_number", f"page_states:{others[0]}"],
                          claim_span=span_around(full_text, others[0], 90) or span)
            return _finish(result)
        if corr and (len(want) >= 2 or strong):
            result.update(score=0.95, reasons=["name_and_address", *corr], claim_span=span)
        else:
            result.update(score=0.8, reasons=["name_only"], claim_span=span)
    elif present_or_host:
        result.update(score=0.3, reasons=["name_partial", f"tokens:{len(present_or_host)}/{len(want)}"], claim_span=span)
    else:
        result.update(score=0.3, reasons=["name_not_found"], claim_span=span)
    return _finish(result)


def _pick_brand(candidates: list, legal_name: Any) -> Optional[str]:
    want = set(name_tokens(legal_name))
    best, best_score = None, -1
    for c in candidates:
        if not c or len(c) > 80 or fold(c).strip() in ("hjem", "home", "forside", "velkommen", "welcome"):
            continue
        score = len(want & set(tokens(c)))
        if score > best_score:
            best, best_score = c.strip(), score
    return best


def _finish(result: dict) -> dict:
    s = result["score"]
    result["status"] = "exact" if s >= 0.9 else "review" if s >= 0.8 else "related_or_uncertain"
    return result
