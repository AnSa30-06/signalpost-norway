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
    # A bare web-server directory listing is not a website: poco-loco.no served "Index of /" and was published.
    "index of /", "directory listing for", "proudly served by litespeed web server",
)
NORWAY_MARKERS = ("norge", "noreg", "norway", "organisasjonsnummer", "org.nr", "orgnr", "org nr")
CONSUMER_MAIL_DOMAINS = {
    "gmail.com", "gmail.no", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.no",
    "live.com", "msn.com", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "online.no", "start.no", "frisurf.no", "c2i.net", "broadpark.no", "getmail.no", "sf-nett.no", "hotmail.co.uk",
}
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


COMPANY_SUFFIX_RE = re.compile(
    r"([A-ZÆØÅ][\w.&/-]*(?:\s+[A-ZÆØÅ0-9][\w.&/-]*){0,3})\s+(AS|ASA|ANS|DA|NUF|SA|BA|KS)\b")


def title_names_another_entity(title_texts: Iterable[str], profile: dict) -> Optional[str]:
    """A page whose own title names a DIFFERENT registered company that contains our name plus more.

    blaauw.no is titled "Home | Einar Blaauw AS" and its text reads "For BLAAUW AS, our wholesale and trading
    company, click here" — a holding company's site that points our company's real homepage somewhere else.
    Name and registered address cannot separate the two: the group shares both, at C. Sundtsgate 1, 5004 Bergen.
    What separates them is that the page introduces itself as the other entity.

    A longer name that the registry lists as one of our own former names is not another entity, so
    "Sandnes Elektriske Forretning AS" never disqualifies SANDNES ELEKTRISKE AS.
    """
    want = set(name_tokens(profile.get("name")))
    if not want:
        return None
    former = {frozenset(name_tokens(h.get("navn")))
              for h in ((profile.get("registry") or {}).get("historiskeNavn") or []) if isinstance(h, dict)}
    supersets = []
    for text in title_texts:
        for m in COMPANY_SUFFIX_RE.finditer(text or ""):
            cand = set(name_tokens(m.group(0)))
            if not cand:
                continue
            if cand == want:
                return None                                  # the title names us; question settled
            if want < cand and frozenset(cand) not in former:
                supersets.append(m.group(0).strip())
    return supersets[0] if supersets else None


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


def _identity_positions(soup: BeautifulSoup, title: str, meta: dict, ld_names: list[str], full_text: str) -> list[tuple[str, str]]:
    """The places where a page names ITSELF, as (label, text). A mention elsewhere in the body is not one of them."""
    footer = " ".join(t.get_text(" ", strip=True) for t in soup.find_all(["footer", "address"]))[:2000]
    heads = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])[:8])[:800]
    author = ""
    for m in soup.find_all("meta"):
        if (m.get("name") or "").lower() in ("author", "publisher", "copyright") and m.get("content"):
            author += " " + m["content"].strip()
    copyright_lines = " ".join(
        span_around(full_text, mk.group(0), 140)
        for mk in list(re.finditer(r"©|\(c\)\s*20\d\d|copyright|alle rettigheter|all rights reserved", full_text, re.I))[:3])
    return [("title", title), ("og:site_name", meta.get("og:site_name", "")), ("og:title", meta.get("og:title", "")),
            *[("json-ld", n) for n in ld_names], ("footer", footer), ("headings", heads), ("author", author.strip()),
            ("copyright", copyright_lines)]


def registered_company_names(text: str) -> list[str]:
    """Every '<Name> AS|ASA|...' style company name written in the text, deduplicated by folded form."""
    seen, out = set(), []
    for m in COMPANY_SUFFIX_RE.finditer(text or ""):
        key = fold(m.group(0)).strip()
        if key not in seen:
            seen.add(key)
            out.append(m.group(0).strip())
    return out


def _labelled_org_present(full_text: str, our_org: str) -> bool:
    """Is OUR organisation number written under an org-number label (Org.nr / organisasjonsnummer / MVA)?"""
    ours = re.sub(r"\D", "", str(our_org or ""))
    for m in ORG_LABEL_RE.finditer(full_text or ""):
        if re.sub(r"\D", "", m.group(1)) == ours:
            return True
    # "NO 987 654 321 MVA" — the VAT form, which carries no word label
    pat = _org_pattern(ours)
    return bool(pat and re.search(r"\bNO\s*" + pat.pattern + r"\s*MVA\b", full_text or "", re.I))


def assess(profile: dict, page, extra_text: str = "") -> dict:
    """Does this fetched page belong to THIS legal entity? Returns score, status, reasons and the proving span.

    Decision table (R1–R6 in docs/REMEDIATION.md). Only ``exact`` (>= 0.9) is ever published as a verified website.

    | name evidence                                | strong corroborator                                    | result |
    |----------------------------------------------|--------------------------------------------------------|--------|
    | our org number, labelled or self-identified  | (the number is the proof)                              | 1.0    |
    | self-identified (identity position or phrase)| postcode / street / domain spells multi-word name      | 0.95   |
    | self-identified                              | place name only, or none                               | 0.8    |
    | full name as a contiguous phrase in the body | postcode / street / domain, and no third party named   | 0.95   |
    | name tokens scattered in the body            | anything                                               | 0.8    |
    | partial or absent                            |                                                        | 0.3    |
    A page that states another organisation number, introduces another registered company in its identity
    positions, or lists many companies, is capped at 0.8 whatever else it carries.
    """
    html = page.text or ""
    soup = BeautifulSoup(html, "lxml")
    text = html_text(html)
    full_text = (text + " " + (extra_text or "")).strip()
    hostname = (urllib.parse.urlsplit(page.final_url or page.url or "").hostname or "").lower()
    try:
        hostname = hostname.encode("ascii").decode("idna")   # "xn--pnerom-hua.no" is "åpnerom.no"
    except Exception:
        pass

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
    positions = _identity_positions(soup, title, meta, ld_names, full_text)
    identity_text = " ".join(v for _, v in positions if v)
    identity_folded = fold(identity_text)
    folded_all = fold(full_text)

    want = name_tokens(profile.get("name"))
    raw = [t for t in tokens(profile.get("name")) if t not in LEGAL_FORMS]
    name_compact = "".join(raw)
    phrase_re = re.compile(r"\b" + r"\s+".join(re.escape(t) for t in raw) + r"\b") if len(raw) >= 2 else None
    in_identity = bool(want) and all(t in set(tokens(identity_text)) for t in want)
    phrase_in_identity = bool(phrase_re and phrase_re.search(identity_folded))
    phrase_in_body = bool(phrase_re and phrase_re.search(folded_all))
    self_identified = in_identity or phrase_in_identity

    brand_candidates = [meta.get("og:site_name"), *ld_names, *clean_title(title)]
    public_brand = _pick_brand(brand_candidates, profile.get("name"))
    aliases = []
    for a in [*ld_names, meta.get("og:site_name"), meta.get("og:title"), *clean_title(title)]:
        if a and a != public_brand and a not in aliases and len(a) <= 80 and set(tokens(a)) & set(want):
            aliases.append(a)
    result = {"score": 0.3, "status": "related_or_uncertain", "reasons": [], "claim_span": "",
              "public_brand": public_brand, "aliases": aliases[:5], "self_identified": self_identified}

    # 0. placeholders and parking pages -----------------------------------------------------------------------
    marker = next((mk for mk in PARKED_MARKERS if mk in folded_all), None)
    if marker:
        result.update(score=0.1, reasons=["parked"], claim_span=span_around(full_text, marker, 80) or marker)
        return _finish(result)

    # 1. organisation numbers on the page ---------------------------------------------------------------------
    pat = _org_pattern(profile.get("organisation_number"))
    ours_match = pat.search(full_text) if pat else None
    others = other_org_numbers(full_text, profile.get("organisation_number"))
    if ours_match:
        span_org = span_around(full_text, ours_match.group(0), 80)
        if len(others) >= 2 and not self_identified:
            # R1: a page carrying several other labelled numbers is a listing, a group index or a client list
            result.update(score=0.8, reasons=["org_number_among_many", f"other_org_numbers:{len(others)}"], claim_span=span_org)
            return _finish(result)
        detail = ("self_identified" if self_identified else
                  "labelled" if _labelled_org_present(full_text, profile.get("organisation_number")) else "bare_digits")
        result.update(score=1.0, reasons=["org_number_on_page", f"org_number:{detail}"] + ([f"other_org_numbers:{len(others)}"] if others else []),
                      claim_span=span_org)
        return _finish(result)
    if others:
        # R: a self-declared different organisation number means this page belongs to another legal entity
        result.update(score=0.8, reasons=["name_match_but_other_org_number", f"page_states:{others[0]}"],
                      claim_span=span_around(full_text, others[0], 90))
        return _finish(result)

    # 2. the page introduces itself as somebody else ------------------------------------------------------------
    id_companies = registered_company_names(identity_text)
    if want:
        other_entity = title_names_another_entity([title, meta.get("og:site_name", ""), meta.get("og:title", "")], profile)
        if other_entity:
            result.update(score=0.8, reasons=["title_names_another_entity", f"page_is:{other_entity}"],
                          claim_span=span_around(full_text, other_entity, 120))
            return _finish(result)
        strangers = [c for c in id_companies if not (set(name_tokens(c)) & set(want))]
        if strangers and not self_identified:
            # R4: a third party's site that merely mentions us (a supplier, an agency, a customer list)
            result.update(score=0.8, reasons=["page_belongs_to_another_company", f"page_is:{strangers[0]}"],
                          claim_span=span_around(full_text, strangers[0], 120))
            return _finish(result)
    all_companies = registered_company_names(full_text)
    if len(all_companies) >= 3 and not self_identified:
        # R5: a directory, a group index, a client list
        result.update(score=0.8, reasons=["page_lists_many_companies", f"companies_named:{len(all_companies)}"],
                      claim_span=span_around(full_text, all_companies[0], 120))
        return _finish(result)

    # 3. name presence and corroboration ------------------------------------------------------------------------
    have_body = set(tokens(full_text))
    present_body = [t for t in want if t in have_body]
    host_compact = re.sub(r"[^a-z0-9]", "", fold(hostname))
    present_or_host = [t for t in want if t in have_body or (len(t) >= 4 and t in host_compact)]
    best_part = max(positions, key=lambda p: (len(set(tokens(p[1])) & set(want)), -len(p[1] or "x")))[1] or title
    span = re.sub(r"\s+", " ", best_part).strip()[:300]
    if want and not set(want) & set(tokens(best_part)):
        span = span_around(full_text, max(want, key=len), 110) or span
    if phrase_in_body and not phrase_in_identity:
        span = span_around(full_text, " ".join(raw), 140) or span

    if not want or len(present_body) < len(want):
        if present_or_host:
            result.update(score=0.3, reasons=["name_partial", f"tokens:{len(present_or_host)}/{len(want)}"], claim_span=span)
        else:
            result.update(score=0.3, reasons=["name_not_found"], claim_span=span)
        return _finish(result)

    corr = _corroborators(profile, folded_all)
    addr_strong = [c for c in corr if c.startswith(("postcode:", "street:"))]
    strong = list(addr_strong)
    norwegian = _norway_signal(folded_all, full_text, hostname)
    host_sans_www = fold(hostname).removeprefix("www.")
    labels = [l for l in host_sans_www.split(".") if l]
    label0 = re.sub(r"[^a-z0-9]", "", labels[0]) if labels else ""
    # The registry's own e-mail address for the company names a domain the company uses (official source).
    reg_email = str((profile.get("registry") or {}).get("epostadresse") or "").strip().lower()
    email_domain = reg_email.rsplit("@", 1)[-1] if "@" in reg_email else ""
    cand_domain = ".".join(labels[-2:]) if len(labels) >= 2 else host_sans_www
    if email_domain and cand_domain and email_domain == cand_domain and email_domain not in CONSUMER_MAIL_DOMAINS:
        corr = sorted(set(corr) | {f"registry_email_domain:{email_domain}"})
        strong.append(f"registry_email_domain:{email_domain}")
    # A domain that spells the whole multi-word name; or the name itself carries its .no ("MITTELVERUM.NO AS").
    name_with_tld = ".".join(raw)
    domain_match = None
    if labels and norwegian:
        if len(raw) >= 2 and len(name_compact) >= 7 and label0 == name_compact:
            domain_match = f"domain_is_legal_name:{labels[0]}"
        elif len(raw) >= 2 and host_sans_www == name_with_tld:
            domain_match = f"domain_is_legal_name:{host_sans_www}"
        elif len(raw) == 1 and len(name_compact) >= 6 and label0 == name_compact and addr_strong:
            # a one-word name spelled by the domain counts only beside a registered postcode or street
            domain_match = f"domain_is_legal_name:{labels[0]}"
    if domain_match:
        corr = sorted(set(corr) | {domain_match})
        strong.append(domain_match)
    host_has_token = any(len(t) >= 4 and t in host_compact for t in want)
    names_others = [c for c in registered_company_names(identity_text) if not (set(name_tokens(c)) & set(want))]

    # R2/R3. Exact needs two INDEPENDENT facts: the page naming itself as us, and a corroborator the page could
    # not produce merely by carrying our name (a registered postcode or street, or a domain that spells the
    # whole multi-word name). The full legal name written out as a phrase is stronger name evidence than
    # scattered tokens, so on a Norwegian page it may stand in for an identity position when the domain also
    # carries a name token — but never for a foreign page, and never when the page names somebody else.
    if self_identified and strong:
        result.update(score=0.95, reasons=["name_and_address", "self_identified", *corr], claim_span=span)
    elif phrase_in_identity and norwegian and (strong or host_has_token) and len(name_compact) >= 7:
        result.update(score=0.95, reasons=["legal_name_phrase_on_page", "in_identity_position", *corr,
                                           *(["host_carries_name_token"] if not strong else [])], claim_span=span)
    elif phrase_in_body and norwegian and len(name_compact) >= 7 and (strong or (host_has_token and not names_others)):
        # the whole name written out, on a Norwegian page whose domain carries part of it and which names no
        # other registered company anywhere it names itself
        result.update(score=0.95, reasons=["legal_name_phrase_on_page", *corr,
                                           *(["host_carries_name_token"] if not strong else [])], claim_span=span)
    elif len(present_body) == len(want) and domain_match and addr_strong and norwegian:
        # one-word name: the domain spells it, the registered postcode or street is on the page, the name is on the page
        result.update(score=0.95, reasons=["name_and_address", "domain_and_address", *corr], claim_span=span)
    elif self_identified or phrase_in_body:
        result.update(score=0.8, reasons=["name_only", *corr], claim_span=span)
    else:
        result.update(score=0.8, reasons=["name_mentioned_only", *corr], claim_span=span)
    return _finish(result)


def _pick_brand(candidates: list, legal_name: Any) -> Optional[str]:
    """The public brand, only when it shares a token with the legal name (R6). Never another company's title."""
    want = set(name_tokens(legal_name))
    best, best_score = None, 0
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
