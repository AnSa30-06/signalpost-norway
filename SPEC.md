# Signalpost Norway agent — internal build spec (frozen interfaces)

Python 3.12+, stdlib + `httpx` is NOT used; use `urllib` via `signalpost.net.Session` only.
Third-party allowed: `beautifulsoup4`, `lxml`, `extruct`, `trafilatura`, `pydantic`, `tldextract`, `rapidfuzz`, `pypdf`.
NO LLM. NO browser by default. Deterministic, offline-testable, every claim carries evidence.

## Package layout (each file owned by exactly one author)

```
signalpost/
  __init__.py
  models.py      # dataclass-like helpers: new_claim, new_evidence, new_error, availability constants   (core)
  net.py         # Session: budgeted, cached, snapshotting HTTP; SSRF guard; robots                     (core)
  registry.py    # Brreg official modules -> claims/evidence                                             (core)
  pipeline.py    # per-company orchestration -> envelope                                                (core)
  cli.py         # `python -m signalpost run ...`                                                       (core)
  identity.py    # exact-entity gate for a fetched homepage                                             (agent A)
  discovery.py   # website candidates: registry url, domain guesses, optional Brave                     (agent A)
  site.py        # crawl verified site: robots/sitemap/targeted pages/JSON-LD/OG/RSS/social/jobs        (agent A)
  jobs.py        # NAV arbeidsplassen job connector                                                     (agent B)
  updates.py     # Brreg oppdateringer dated events                                                     (agent B)
  synthesis.py   # deterministic summary + cannot_establish                                             (agent B)
  refresh.py     # diff previous envelope vs current -> typed changes                                    (agent B)
tools/
  build_site.py  # envelopes.jsonl -> site/index.html (+ data)                                          (agent C)
docs/*.md, README.md                                                                                    (agent C)
tests/                                                                                                  (agent B + A)
```

## Availability states (exact strings)
`available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`

## Source classes (exact strings)
`official_registry`, `official_accounts`, `official_roles`, `official_subunits`, `official_updates`,
`company_owned`, `official_job_board`, `search_candidate` (candidates only — never evidence for a claim)

## Claim
```json
{"id":"c-12","section":"web","field":"official_website","value":"https://example.no/",
 "availability":"available","confidence":0.99,"evidence_ids":["ev-3"],
 "reporting_period":null,"effective_date":null,"note":null}
```
Sections (exact): `identity`, `accounts`, `leadership`, `workplaces`, `web`, `hiring`, `activity`.
When a field was checked and nothing exists, emit ONE claim with `value:null`, `availability:"not_available"` and a `note`.
When blocked (robots / 403 / policy) -> `availability:"blocked"`. When the source does not apply
(e.g. accounts for a form that never files) -> `not_applicable`. When identity could not be proven -> `ambiguous`
(value MAY hold the unproven candidate, note says why). Network/parse failure -> `failed`.

## Evidence
```json
{"id":"ev-3","source_url":"https://example.no/om-oss","final_url":"https://www.example.no/om-oss/",
 "source_class":"company_owned","retrieved_at":"2026-09-09T12:00:04Z","http_status":200,
 "content_sha256":"<hex>","snapshot_path":"snapshots/<hex>.html","claim_span":"Org.nr. 987 654 321",
 "extraction_method":"footer_regex","reporting_period":null}
```
`claim_span` = the literal text (<=300 chars) that supports the claim. Always set it.

## Session API (net.py) — the ONLY way to do HTTP
```python
from signalpost.net import Session, FetchResult
s = Session(run_dir, max_total_requests=1950, per_company_cap=22, timeout=12, user_agent=...)
r: FetchResult = s.get(url, company=orgnr, kind="html"|"json"|"xml"|"text"|"pdf", max_bytes=2_000_000, robots=True)
# r.ok (bool), r.status (int|None), r.url (requested), r.final_url, r.redirects (list[str]),
# r.body (bytes), r.text (str, decoded), r.sha256, r.retrieved_at (iso Z), r.snapshot_path (relative),
# r.error (str|None), r.blocked (bool: robots/403/451), r.budget_exhausted (bool), r.cached (bool)
# r.json() -> parsed or None
s.requests_used(company) -> int ; s.total_requests -> int
```
Every attempt counts (redirect hops count). Cache hits (same URL in same run) are free. Robots is
fetched once per host and cached. All snapshot bytes are written to `<run_dir>/snapshots/<sha256>.<ext>`.

## Module contracts

### identity.assess(profile, page: FetchResult, extra_text: str = "") -> dict
`profile` = the universe row dict (`organisation_number`, `name`, `legal_form`, `municipality`, ...)
plus `registry` (live enheter JSON or None). Returns:
```json
{"score":1.0,"status":"exact"|"review"|"related_or_uncertain","reasons":["org_number_on_page"],
 "claim_span":"Org.nr 987 654 321","public_brand":"Example","aliases":["Example AS"]}
```
Rules: org number (with/without spaces, "NO" prefix, "MVA" suffix) on page -> 1.0. Otherwise all name
tokens (folded, legal-form stopwords removed) present in title/og:site_name/JSON-LD name/footer AND one
corroborator (registry postcode/street/municipality or phone on page) -> 0.95. Tokens present but no
corroborator -> 0.8 (`review`). Parked/for-sale page -> 0.1. Only `exact` (>=0.9) is publishable.

### discovery.candidates(profile, session) -> list[dict]
Ordered list of `{"url":..., "origin":"registry"|"domain_guess"|"brave", "note":...}`. Domain guesses are
derived from the legal name (folded ø->o, å->a, æ->ae; strip legal-form words; join with "" and "-"; TLD .no
then .com), max 4 guesses. Brave only if `BRAVE_API_KEY` env set; results are transient and never stored
as evidence. Never returns directories/socials (proff, 1881, gulesider, purehelp, linkedin, facebook ...).

### site.crawl(profile, session, homepage: FetchResult, identity: dict, page_budget: int) -> dict
Crawls the verified site. Returns:
```json
{"claims":[...], "evidence":[...], "pages":[{"url":..,"kind":"about|contact|team|careers|news|press|sitemap|rss|homepage","status":200}],
 "errors":[...]}
```
Fields it may emit (section in parentheses): `official_website`(web), `public_brand`(identity),
`website_title`(web), `website_description`(web), `social_profile`(web; value {"platform","url"}) one claim
per platform, `contact_email`(web), `contact_phone`(web), `site_leader`(leadership; value {"name","title"}),
`site_location`(workplaces; value {"name","address"}), `job_posting`(hiring; value {"title","url","date_posted","valid_through","location"})
from JSON-LD JobPosting or careers page, `news_item`(activity; value {"title","url","date"}) from RSS/JSON-LD/news page,
`sitemap_lastmod`(activity; value ISO date of newest lastmod). Use JSON-LD first, then OG, then DOM, then text.
Claim ids must be unique within the result: prefix `site-`. Evidence ids prefix `evs-`.

### jobs.fetch(profile, session) -> dict  `{"claims":[...],"evidence":[...],"errors":[...]}`
GET `https://arbeidsplassen.nav.no/stillinger/api/search?q=<legal name>&size=25`. Accept a hit only when
`_source.businessName` or `_source.employer.name`, folded, equals the legal name exactly (or legal name minus
legal-form suffix equals folded businessName). Emit one `job_posting` claim per accepted ad
(value {"title","url":"https://arbeidsplassen.nav.no/stillinger/stilling/<uuid>","date_posted","valid_through","location","source":"nav"})
plus one `active_job_count`(hiring) claim. Zero accepted ads after a successful search -> `active_job_count` value 0,
availability `available`, note "checked NAV, no ads matched exact legal name". Search failure -> `failed`.
Ids prefix `job-`/`evj-`.

### updates.fetch(profile, session) -> dict
GET `https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter?organisasjonsnummer=<org>&size=20`.
One `registry_update`(activity) claim per event with value {"date","change_type"}; ids prefix `upd-`/`evu-`.

### synthesis.build(envelope) -> dict
Deterministic English text from claims only. Returns
`{"summary": str, "what_it_does": str|None, "size": str|None, "leadership": str|None, "footprint": str|None,
  "hiring": str|None, "recent_activity": str|None, "what_changed": str|None, "cannot_establish": [str,...]}`.
Never state a value that is not in an `available` claim. `cannot_establish` lists every section/field that is
`not_available`/`blocked`/`ambiguous`/`failed` in plain words.

### refresh.diff(previous_envelope: dict|None, current_envelope: dict) -> list[change]
Stable claim key = (field, normalised value key: url / name / platform / period). Change types:
`new_website`, `changed_website`, `new_social_profile`, `new_role`, `removed_role`, `new_filing`,
`changed_financials`, `new_job`, `closed_job`, `new_location`, `removed_location`, `new_news`,
`changed_description`, `new_registry_update`, `availability_changed`. `materiality`: `material`|`minor`.
Change record:
```json
{"field":"job_posting","change_type":"new_job","materiality":"material","previous_value":null,"current_value":{...},
 "first_observed":"<iso>","last_observed":"<iso>","evidence_ids":["evj-1"],"previous_evidence_ids":[]}
```
If previous is None -> `[]` and envelope.run.refresh.baseline = true.

## Envelope (pipeline.py assembles)
```json
{"organisation_number":"...","schema_version":"1.0",
 "run":{"run_id":"...","started_at":"...","completed_at":"...","terminal_status":"completed"|"completed_with_errors"|"failed",
        "agent_version":"<git sha or version>","previous_run_id":null,"refresh":{"baseline":true,"previous_snapshot":null}},
 "identity":{"legal_name":..,"legal_form":..,"municipality":..,"industry_code":..,"industry_label":..,"public_brand":..,"official_website":..},
 "sections":{"identity":"available","accounts":"available","leadership":"available","workplaces":"available","web":"ambiguous","hiring":"available","activity":"available"},
 "claims":[...],"evidence":[...],"changes":[...],"errors":[...],
 "operations":{"requests":17,"runtime_ms":8120,"third_party_cost_usd":0,"bytes":123456,"budget_exhausted":false},
 "synthesis":{...}}
```
`terminal_status` is `completed` when every section has a non-`failed` state; `completed_with_errors` when any
section `failed`; `failed` only if the registry anchor itself failed. There is ALWAYS exactly one envelope per input.
