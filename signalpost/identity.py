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
)
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
    have = set()
    for _, part in parts:
        have.update(tokens(part))
    host_compact = re.sub(r"[^a-z0-9]", "", fold(hostname))
    present = [t for t in want if t in have or (len(t) >= 4 and t in host_compact)]
    best_part = max(parts, key=lambda p: (len(set(tokens(p[1])) & set(want)), -len(p[1] or "x")))[1] or title
    span = re.sub(r"\s+", " ", best_part).strip()[:300]
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
        if len(raw) >= 2 and len(name_compact) >= 7 and labels and re.sub(r"[^a-z0-9]", "", labels[0]) == name_compact:
            corr = sorted(set(corr) | {f"domain_is_legal_name:{labels[0]}"})
            strong = strong or [f"domain_is_legal_name:{labels[0]}"]
        if corr and (len(want) >= 2 or strong):
            result.update(score=0.95, reasons=["name_and_address", *corr], claim_span=span)
        else:
            result.update(score=0.8, reasons=["name_only"], claim_span=span)
    elif present:
        result.update(score=0.3, reasons=["name_partial", f"tokens:{len(present)}/{len(want)}"], claim_span=span)
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
