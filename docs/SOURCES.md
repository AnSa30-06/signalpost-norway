# Sources

Every source the agent touches, its endpoint, its rights basis and the access rules applied. Nothing else is fetched.
All HTTP goes through `signalpost.net.Session`, which enforces the SSRF guard, robots.txt, the timeout, the byte cap and the budgets.

## Official Norwegian records (identity anchor)

Provider: Brønnøysundregistrene. Licence: Norwegian Licence for Open Government Data (NLOD) 2.0. No key, no login.

| Source class | Endpoint | Used for |
|---|---|---|
| `official_registry` | `https://data.brreg.no/enhetsregisteret/api/enheter/{org}` | legal name, legal form, addresses, industry code, registered employee count, registry website field (`hjemmeside`) |
| `official_accounts` | `https://data.brreg.no/regnskapsregisteret/regnskap/{org}` | latest filed annual accounts: revenue, operating result, annual result, assets, equity, debt, period |
| `official_roles` | `https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller` | daglig leder, board roles, other registered roles |
| `official_subunits` | `https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}` | registered workplaces (subunits) with addresses |
| `official_updates` | `https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter?organisasjonsnummer={org}&size=200` | dated registry change events |
| `official_accounts` | `https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar` | years with an annual-account copy on file; one request per company, paced to about 30 a minute across the whole run |

Official data anchors identity. It does not, by itself, identify the public brand or website.
The universe file supplied by Builderr (`signalpost-company-universe-2025.jsonl.gz`) is the frozen registry snapshot
used to seed each company before any request; live registry responses are still fetched and stored as evidence.

## NAV job vacancy feed (official job board)

Provider: NAV, the Norwegian Labour and Welfare Administration. Terms: `https://arbeidsplassen.nav.no/vilkar-api`
("Alle kan bruke tenesta" - anyone may use the service; free; republishing ads is allowed; an ad must not be shown
once inactive; contact details fall under GDPR). Documentation: `https://navikt.github.io/pam-stilling-feed/`.

| Source class | Endpoint | Rule |
|---|---|---|
| `official_job_board` | `https://pam-stilling-feed.nav.no/api/v1/feed` with `If-Modified-Since` = now minus `--nav-days` (60), then `next_url` pages of 1,000 events; bearer token | one scan per run (about 20-40 requests), fetched as three time-window chains; the latest event per ad is kept and active ads are indexed by employer name |
| `official_job_board` | `https://pam-stilling-feed.nav.no/api/v1/feedentry/<uuid>` | fetched only for ads whose employer name matches the legal name (at most 8 per company); an ad is published only when `ad_content.employer.orgnr` equals the organisation number, or equals a subunit number the official registry lists under this entity (Norwegian ads are posted by the establishment, not the legal entity) |

Token: `NAV_FEED_TOKEN` (a private consumer token from NAV, requested by email to nav.team.arbeidsplassen@nav.no) or,
when unset, the public experimentation token published at `https://pam-stilling-feed.nav.no/api/publicToken`
(fetched once per run; it rotates at irregular intervals). Published per ad: title, published date, expiry, work
location city, extent, engagement type, position count and the public ad link. Never published: contact persons,
phone numbers, e-mail addresses, application URLs, descriptions.

Fallback when the feed cannot be scanned (`--no-nav-feed`, or token/feed failure): the site search API
`https://arbeidsplassen.nav.no/stillinger/api/search?q=<legal name>&size=25`, paced at one request per 3 s
(NAV answers HTTP 429 to bursts, and that block has lasted more than ninety minutes), accepting a hit only when the
employer name, folded, equals the legal name exactly. The search API carries no organisation number, which is why
the feed is the primary route.

## Company-owned websites

Source class `company_owned`. Only a site that passed the exact-entity gate (IDENTITY_RESOLUTION.md) is crawled and published.

Access rules, all enforced in `signalpost/net.py`:

- `robots.txt` fetched once per host and obeyed for both the agent's user agent and `*`. A robots.txt that cannot be fetched (401, 403, 404) is unavailable under RFC 9309 and imposes no restrictions; a redirect onto another host is checked against that host's robots.txt too.
- Identified user agent: `signalpost-norway-agent/1.0 (+https://github.com/AnSa30-06/signalpost-norway; research crawler; contact via repo)`.
- 12 second timeout, 2 MB per response, at most 5 redirect hops, one retry on 5xx or timeout, a 90 s host cooldown after three consecutive 429s.
- About 10 pages per site: homepage, sitemap, up to 7 targeted pages (about, contact, team, careers, news, press), RSS.
- Only the registered domain of the verified site. No off-domain links are followed.
- Per-host concurrency limit (4 by default; 8 for data.brreg.no; 1 for arbeidsplassen.nav.no, paced at one request per 3 s).

Social or video profile URLs (LinkedIn, Facebook, Instagram, YouTube, X, TikTok) are recorded only when the
verified site links to them through `sameAs` in JSON-LD or an anchor. They are labelled `company_owned`,
with the site page as evidence. The destination platform is never fetched.

## Search provider (candidates only)

Brave Search API, used only when the environment variable `BRAVE_API_KEY` is set. One query per company that
has no usable registry website. Results are labelled `search_candidate`. They propose website candidates and
are never referenced by a published claim. The candidate site itself is then fetched and must pass the identity gate.
Paid plan list price is about $0.50 per 100 companies. Without the key, the third-party cost is $0.

## URL safety

`signalpost.net.assert_public_url` rejects, before any request and again on every redirect:

- schemes other than `http` and `https`;
- credentials in the URL;
- `localhost`, `.local`, `.internal`, `.localhost`, `.lan`, `.home` hosts and malformed hosts;
- IP literals that are not global, and hostnames that resolve to any non-global address (loopback, private, link-local, multicast, reserved).

## Not used

LinkedIn, Meta (Facebook, Instagram), Glassdoor, Indeed, Google (search pages, Maps, News), Proff, 1881, Gulesider,
Purehelp and similar directories. None is fetched. Directory and social hosts are also excluded from website candidates.
See LIMITATIONS.md for what this costs in coverage.
