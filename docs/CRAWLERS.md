# Crawlers: connectors, budgets and fallback rules

All HTTP goes through `signalpost.net.Session`. There is no second HTTP path. The session charges every attempt
(including each redirect hop and each retry) against a per-company cap and a global cap, writes every response
body to `snapshots/<sha256>.<ext>`, and logs every attempt to `requests.jsonl`.

## Session settings

| Setting | Value | Where |
|---|---|---|
| global request cap | 1,950 (`--max-requests`) | 50 below the evaluator's 2,000 to leave room for counting differences |
| per-company cap | 26 (`--per-company-cap`) | the global cap of 1,950 is the hard limit; companies without a website use about 11, so the batch average stays near 15 |
| timeout | 12 s per request | |
| response cap | 2 MB per page (200 KB for robots.txt) | bytes beyond the cap are dropped |
| redirects | at most 5 hops, each counted, each re-checked by the public-URL guard | |
| retries | 429: up to 3 attempts with 3 s, 6 s waits, and after 3 consecutive 429s from one host that host is put on a 90 s cooldown during which it is not contacted (and nothing is charged); 500/502/503/504: one retry after 0.8 s; timeout or reset: one retry after 0.5 s; only if budget remains | |
| cache | same URL inside one run is served from memory and costs nothing | |
| concurrency | `--workers 8` companies in parallel; per host 4 connections (8 for data.brreg.no, 1 for arbeidsplassen.nav.no with at least 3 s between request starts) | |
| user agent | `signalpost-norway-agent/1.0 (+https://github.com/AnSa30-06/signalpost-norway; research crawler; contact via repo)` | |
| Accept-Language | `nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6` | |

## robots.txt

Fetched once per origin and cached for the run. A URL is fetched only if both the agent's user agent and `*` are allowed.
If robots.txt cannot be fetched (401, 403, 404, network error), it is "unavailable" in the sense of RFC 9309
§2.3.1.3 and imposes no restrictions; the fetch proceeds and the attempt is recorded in `requests.jsonl`. A site
that refuses this crawler refuses the page itself, and that answer is recorded as `blocked`. A redirect onto a
different host is checked against that host's robots.txt before it is followed.
Official APIs (data.brreg.no, arbeidsplassen.nav.no) are fetched with the robots check on as well.

## Connectors

| Connector | Module | Requests | Endpoint | Kind | On failure |
|---|---|---|---|---|---|
| entity | `registry.py` | 1 | `enhetsregisteret/api/enheter/{org}` | json | terminal status `failed` if this one fails; other connectors still run |
| accounts | `registry.py` | 1 | `regnskapsregisteret/regnskap/{org}` | json | 404 → `not_available` (or `not_applicable` by legal form); other → `failed` |
| roles | `registry.py` | 1 | `enhetsregisteret/api/enheter/{org}/roller` | json | `failed` |
| subunits | `registry.py` | 1 | `enhetsregisteret/api/underenheter?overordnetEnhet={org}` | json | `failed`; zero subunits → `not_available` |
| updates | `updates.py` | 1 | `enhetsregisteret/api/oppdateringer/enheter?organisasjonsnummer={org}&size=200` (the API returns oldest-first; the newest 20 events are kept) | json | `failed` |
| website candidates | `discovery.py` | 0 (Brave: 1, optional) | registry field, domain guesses, Brave | json | Brave error → skip Brave, no cost |
| candidate probes | `identity.py` | up to 3 pages, plus robots.txt per new host; a candidate whose TLS is broken is retried once over plain http | homepage of each candidate | html | probe error → next candidate |
| site crawl | `site.py` | sitemap 1-2, targeted pages up to 7, RSS 1 | verified site only | html/xml | page error → that page's claims are skipped; section state from the pages that worked |
| NAV feed scan (once per run) | `navfeed.py` | about 20-40 for the whole batch, charged to the shared key `_navfeed` (exempt from the per-company cap, counted in the global cap) | `pam-stilling-feed.nav.no/api/v1/feed` + `next_url` pages | json | scan failure → search-API fallback |
| NAV ad confirmation | `navfeed.py` | 0-8 per company | `pam-stilling-feed.nav.no/api/v1/feedentry/<uuid>` | json | a record that cannot be fetched makes the count `failed` (unknown), never zero |
| NAV search fallback | `jobs.py` | 1 | `arbeidsplassen.nav.no/stillinger/api/search?q=<legal name>&size=25` | json | `failed` |

Per-company plan under the 26-request cap:

```
5 official  +  0-8 NAV ad confirmations (usually 0-1)  +  up to 3 candidate probes (each with its robots.txt)
+  1-2 sitemap  +  up to 7 targeted pages  +  1 RSS   =  up to 24 in the common case; plus 20-40 requests per
batch for the shared NAV feed scan
```

Measured on the 1,000-company submission run (2026-09-09): see `submission/run-report-1000.json` for the mean
and maximum requests per company. Companies with no website stop at about 11 requests.

A candidate on a new host costs its own robots.txt fetch. Redirect hops count. So a company with several wrong
candidates, or a site that redirects twice, can reach the cap before the targeted pages are fetched. When that
happens the remaining fetches return `budget_exhausted`, the affected claims are `not_available` with a note, and
`operations.budget_exhausted` is `true`. Official sources run first, so the identity anchor is never the part that is cut.

## Failure to state mapping

`signalpost.models.state_from_fetch`:

| Fetch result | State |
|---|---|
| `blocked` (robots, 401, 403, 451) | `blocked` |
| `budget_exhausted` | `not_available` |
| HTTP 404 or 410 | `not_available` |
| anything else (network, timeout, 5xx after retry, bad body) | `failed` |

Every non-`available` outcome is also written as an error record with the stage and the source URL.

## Site crawl rules

- Only the verified site (identity gate `exact`), only its registered domain. Off-domain links are recorded as text, never fetched.
- Page order: homepage (already fetched by the probe), sitemap (from robots.txt `Sitemap:` or `/sitemap.xml`), then targeted paths in priority order: about (`/about`, `/om-oss`, `/om`), contact (`/contact`, `/kontakt`), team (`/team`, `/ledelse`, `/ansatte`), careers (`/careers`, `/jobs`, `/jobb`, `/karriere`, `/ledige-stillinger`), news (`/news`, `/nyheter`, `/aktuelt`, `/presse`), then RSS if the homepage or sitemap points to one. Paths found in the sitemap take priority over guessed paths.
- About 10 pages per site in total.
- Extraction per page: JSON-LD (`Organization`, `LocalBusiness`, `JobPosting`, `NewsArticle`, `Person`), then OpenGraph, then DOM, then text.
- Social profiles come from `sameAs` and anchors on the verified site only.

## Not implemented (no fallback)

- No browser rendering. A JavaScript shell yields its static HTML only.
- No PDF parsing of annual-account copies. Financials come from the JSON accounts API.
- No leader-to-brand bridge through third-party profile pages.
- No unofficial clients for any platform.

## Snapshots

Every response body is written once to `<run_dir>/snapshots/<sha256>.<ext>` (`html`, `json`, `xml`, `txt`, `pdf`;
an HTML request that returns JSON is stored as `.json`). Identical bytes share one file. Each run has its own directory, so a
refresh never overwrites an earlier snapshot.
