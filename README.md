# Signalpost Norway agent

Turns a Norwegian organisation number into a company profile that a reader can check.
Every published value points to a stored snapshot and to the literal text span that supports it.

- No LLM anywhere. Extraction, identity checks and the summary text are deterministic rules.
- No browser. Static HTTP only, through one budgeted session (`signalpost/net.py`).
- One terminal envelope per input, always, even when every source is missing or blocked.
- Absence is a state (`not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`), never a zero.

Built for the Builderr Signalpost competition, Round 1 (2026).

## What a profile contains

Seven sections, each with its own availability state:

| Section | Source | Content |
|---|---|---|
| `identity` | Brønnøysundregistrene (Enhetsregisteret) | legal name, legal form, address, industry, registered employees, public brand |
| `accounts` | Regnskapsregisteret | latest filed annual accounts and the years available |
| `leadership` | official roles endpoint, verified company site | registered roles (daglig leder, board), leaders named on the site |
| `workplaces` | official subunits, verified company site | registered subunits, locations named on the site |
| `web` | company-owned site (identity-gated) | verified official website, title, description, contact details, linked social profiles |
| `hiring` | NAV job vacancy feed, verified company site | job ads confirmed by the employer's organisation number in NAV's record, active job count |
| `activity` | Brreg oppdateringer, site news/RSS/sitemap | dated registry events, news items, newest sitemap date |

Plus: claim-level evidence, typed changes since the previous run, errors, operations counters and a deterministic synthesis.
The schema is in [docs/DATA_SCHEMA.md](docs/DATA_SCHEMA.md).

## Quick start

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen
python -m signalpost run \
  --input batch.jsonl \
  --universe signalpost-company-universe-2025.jsonl.gz \
  --out out/run-001 --run-id run-001 --expected-count 100
python -m signalpost validate --envelopes out/run-001/envelopes.jsonl --expected-count 100
python -m signalpost site --envelopes out/run-001/envelopes.jsonl --out site/
```

`uv sync --frozen` needs `uv.lock` in the repository. If the lockfile is missing, run `uv lock` once and commit it.
A frozen submission must include the lockfile.

`signalpost-company-universe-2025.jsonl.gz` is the public universe file from Builderr. Download it into the
repository root, or pass another path with `--universe`. The agent reads legal name, legal form, municipality
and industry from it before it makes any request.

Input formats:

- `.jsonl`: one JSON object per line with an `organisation_number` field (nine digits).
- `.txt`: one nine-digit organisation number per line.

## The one evaluator command

```bash
./run.sh batch.jsonl out/run-001 [previous/envelopes.jsonl]
```

`run.sh` runs `uv sync --frozen`, then `python -m signalpost run`, then `python -m signalpost validate`.
It counts the non-empty input lines and passes that number as `--expected-count`, so the validate step fails
if the run did not produce exactly one envelope per input. The optional third argument enables refresh mode.

Environment variables that `run.sh` reads (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `SIGNALPOST_UNIVERSE` | `signalpost-company-universe-2025.jsonl.gz` | path to the universe file |
| `SIGNALPOST_RUN_ID` | the output directory name | run id written into every envelope |
| `SIGNALPOST_WORKERS` | `24` | parallel companies |
| `SIGNALPOST_TIME_BUDGET_MIN` | `40` | the run finishes inside this many minutes (see Budget) |
| `SIGNALPOST_MAX_REQUESTS` | 30 × input count | run-wide request cap |
| `BRAVE_SEARCH_API_KEY` | unset | Brave Search API key: website discovery for companies the registry lists no website for (`BRAVE_API_KEY` also accepted) |
| `SIGNALPOST_BRAVE_QPS` | `1` | Brave queries per second the plan allows |

For a local run, copy `.env.example` to `.env` and put the key there. `.env` is git-ignored and read at start-up;
a variable already set in the environment wins.

Docker alternative:

```bash
docker build -t signalpost .
docker run --rm -v "$PWD:/data" signalpost \
  --input /data/batch.jsonl --universe /data/signalpost-company-universe-2025.jsonl.gz \
  --out /data/out/run-001 --run-id run-001 --expected-count 100
```

## Output files

All outputs go into the directory given by `--out`. Each run writes its own directory. Nothing from a previous run is overwritten.

| File | Content |
|---|---|
| `envelopes.jsonl` | one envelope per input, in input order |
| `run-report.json` | `run_id`, `started_at`, `completed_at`, `inputs`, `envelopes`, `terminal_status_counts`, `section_state_counts`, `requests`, `bytes`, `third_party_cost_usd`, `runtime_ms`, `p50_ms`, `p95_ms`, `budget_exhausted_companies`, `previous_run_id`, `changes_by_type` |
| `requests.jsonl` | every HTTP attempt: company, url, final url, status, attempts, redirects, error, sha256, retrieved_at, elapsed_ms |
| `snapshots/<sha256>.<ext>` | raw bytes of every response, named by content hash |
| `manifest.txt` | the organisation numbers, in input order |
| `checkpoint.jsonl` | progress file used to resume an interrupted run |

## Budget

Builderr's official batch is now 1,000 companies (it may grow to 1,100) with a fixed time and resource budget per
run, and a run that times out is not scored. The published pages do not state the numbers, so the agent enforces
its own:

- **Time.** `--time-budget-min` (default 40 in `run.sh`). The registry's filing-years endpoint is paced at about 28
  requests a minute for the whole process, which alone takes 35 minutes per 1,000 companies. In the last four
  minutes no worker waits for a paced slot; in the last five minutes no search query is spent; in the last two and a
  half minutes a verified site gets its homepage only; in the last minute no website is looked for. Each skipped
  step says `not checked this run` in its note, and the refresh diff does not report it as a change. Every company
  still gets a terminal envelope.
- **Requests.** `run.sh` sets the run-wide cap to 30 per input company and the per-company cap to 30. Measured
  average: 10.2 per company. When a company hits its cap, the remaining sources are marked `not_available` with a
  note, the envelope is still written, and `operations.budget_exhausted` is `true`.
- **Cost.** $0 without a search key. With `BRAVE_SEARCH_API_KEY` set, one query per company that the registry and
  the domain guesses leave without a website, a second only if the first returns no usable candidate: at Brave's
  list price of $5 per 1,000 queries, an estimated $4 to $7 per 1,000-company batch (about 880 of 1,000 companies
  reach the search step). The estimate is replaced by a measurement once a run with a key has been made. `operations.third_party_cost_usd` on every
  envelope and `third_party_cost_usd` plus `search` in `run-report.json` report the actual spend. A run-wide cap
  (`--search-max-queries`, default 2,500) bounds it.

See [docs/CRAWLERS.md](docs/CRAWLERS.md) for the per-company request plan.

## Revision 2 (2026-09-12): response to the first evaluation report

Builderr's provisional score for the first submission was 74.67/100 with qualification blocked by two
wrong-company website publications. Every fault in that report is addressed in this revision, with a regression
test per rule; the point-by-point response is in [docs/REMEDIATION.md](docs/REMEDIATION.md). In one line each:

- **Website identity**: the page must name the company where a page names itself, plus one independent corroborator; a page that carries other organisation numbers, states another number as its own, introduces another registered company, or lists many companies is never published as verified.
- **Name changes**: identity scalars are now diffed (`changed_name`, `changed_address`, `changed_status`, …).
- **New filings without revenue**: the filing trigger moved from the revenue claim to the reporting period itself.
- **More evidence and clearer answers**: up to three prior accounting periods, a trend, risk flags, a sentence saying why the website counts as verified, and eleven standard questions answered only from claims.
- **Verbatim evidence (2026-09-13)**: every `claim_span` is now an excerpt of the bytes it cites, checked for every evidence record of the submission run by `eval/audit_artifact.py`; an audit of the earlier packaging had found 57% of registry spans were compact re-serialisations rather than excerpts (no value was wrong). A registry field the record does not carry is `not_available`, never `false` or borrowed from the seed file.

## Revision 3 (2026-09-13): response to the second evaluation report

The second report scored revision 2 at 69.57/100 with one wrong-company website: fjords.com for THE FJORDS DA, a
travel-guide page that names the village the company is registered in. The identity gate now publishes a website
on exactly three kinds of evidence and no others: the organisation number on the page; the exact legal name
together with the postcode written with its town or a street line with a house number; or a domain the company
itself filed with the registry. A domain that spells the name, a name without an address, a bare postcode or a
bare place name never reach `exact`. Details and the measured cost are in
[docs/IDENTITY_RESOLUTION.md](docs/IDENTITY_RESOLUTION.md) and [docs/REMEDIATION.md](docs/REMEDIATION.md).

## Measured

Revision 4 run, 2026-09-28, code commit `781b8a8`, **without a search key** (`submission/run-report-1000.json`,
`submission/smoke-100/`, `eval/report.json`). The numbers with Brave search are added when a key-backed run exists.

| Measure | Value |
|---|---|
| envelopes / inputs | 1,000 / 1,000, zero validation problems |
| wall clock | 36.2 min at 24 workers with `--time-budget-min 40`; the time guard skipped the paced filing-years step for the last 20 companies, each marked `not checked this run` |
| requests | 9,877 (120 for the once-per-run NAV feed scan, then 9.8 per company) |
| third-party cost | $0 (no search key) |
| identity / accounts / leadership / workplaces / activity / hiring | 1,000 / 999 / 999 / 1,000 / 1,000 / 1,000 sections available |
| web | 110 verified exact, 132 ambiguous, 731 no site found, 21 unreachable, 6 blocked |
| evidence | 43,811 evidence records, 0 spans not verbatim; all 49,766 available claims carry `source_url`, `retrieved_at`, `content_sha256` and `claim_span` on the claim itself; all 110 published websites clean on the four wrong-company traps and re-gated `exact` |
| hiring | 4 job postings (NAV feed items confirmed by organisation number); careers pages are no longer hiring facts |
| gold set (46 hand-labelled rows) | precision 1.000, recall 0.679, zero wrong-company publications |
| refresh against revision 3 (15 days earlier) | real registry movement (59 employee counts, 27 new and 25 removed roles, 111 registry updates, 21 industry codes); 10 websites lost verification, every one to the site itself (403s, robots, resets, a redirect loop, one page now stating another organisation number) |

**100-company smoke test, measured** (`./run.sh work/judge-100.jsonl out/smoke-100-r4 <revision-4 envelopes>`):
100 of 100 envelopes, zero validation problems, 1,153 requests, 5.7 minutes including `uv sync`, $0; **zero change
records** against the previous day. Report: `submission/smoke-100/run-report.json`, audit: `submission/smoke-100/audit.txt`.

**Judge-shaped run, revision 2, measured** (2026-09-13, `./run.sh` on a fixed random 100 with the packaged
envelopes as the previous day, from a fresh clone with no seed file): 100 of 100 envelopes, zero validation
problems, 1,179 requests against the 1,950 cap, 9.6 minutes at 8 workers against the 45-minute cap, $0; a
second day against the first: zero change records.

**Judge-shaped run, revision 3, measured** (2026-09-13, `./run.sh work/judge-100.jsonl out/judge-100-r3 <revision-3
envelopes>`, the same fixed random 100 with the packaged revision-3 envelopes as the previous day): 100 of 100
envelopes, zero validation problems; 1,185 requests against the 1,950 cap; 9.8 minutes at 8 workers plus one minute of
`uv sync`, against the 45-minute cap; p50 12.1 s, p95 117.8 s; $0. **Zero change records** and every section state
identical to the package. 7 of the 100 carry a verified website under the revision-3 gate (10 under revision 2).
`eval/audit_artifact.py` on that run: 4,216 evidence records, 0 spans not verbatim, all 7 published websites clean.

**Hand-audited accuracy** over 230 companies across two rounds, 46 of them labelled from the page itself
(`eval/gold.jsonl`, method in [docs/EVAL.md](docs/EVAL.md)):

| Measure | Value |
|---|---|
| exact-website precision on the labelled rows | 1.000 |
| website recall on the labelled rows | 0.964 |
| wrong-company publications on the labelled rows | 0 |

Read those three numbers for what they are. Builderr's first report found two wrong-company publications in
**their** 100-company batch while our labelled rows showed none; a perfect figure on a self-chosen sample is
not zero in the wild. Revision 2 closes every route by which the previous gate could still publish a wrong
site, and the re-measure over the previous run's stored pages withdrew four publications that a reader would
call a group, a chain or a listing page (see [docs/REMEDIATION.md](docs/REMEDIATION.md)).

Recall against the evaluator's pooled union of every entrant's discoveries is **not measured**; it cannot be
computed locally. Website coverage of 14.5% reflects the population: a random draw from the registry is mostly
holding companies and property entities with no website at all.

## Secrets
## Secrets

Two optional secrets, both read from environment variables only (or a git-ignored `.env` for local runs).
`NAV_FEED_TOKEN`: a private consumer token for NAV's job vacancy feed; without it the agent fetches NAV's public
experimentation token once per run. `BRAVE_SEARCH_API_KEY`: without it the agent never calls Brave. Brave results
are held in memory only: no result, rank, title, snippet or query text is written to disk, and the request log
records the endpoint without its query. A result URL is only a candidate: it is fetched again and must pass the
exact-entity gate, and its host must carry a distinctive word of the legal name.
No other key, token or account is used. Nothing is written outside `--out` and `--out/../site` (or the `--out` you give to `site`).

## Source rights

- Brønnøysundregistrene open data (Enhetsregisteret, Regnskapsregisteret, roles, subunits, oppdateringer): Norwegian Licence for Open Government Data (NLOD) 2.0.
- NAV job vacancy feed (pam-stilling-feed.nav.no): official API, bearer token, terms at arbeidsplassen.nav.no/vilkar-api (anyone may use it, free, republishing allowed, inactive ads must not be shown, contacts never published).
- Company-owned websites: `robots.txt` respected per RFC 9309, identified user agent, 12 s timeout, 2 MB per page, about 10 pages per site, registered domain only.
- Brave Search API: candidate discovery only, under Brave's API terms; results are not stored.
- LinkedIn, Meta, Glassdoor, Indeed and Google are not crawled. A social profile URL is recorded only when the verified company site links to it.

Details and endpoints: [docs/SOURCES.md](docs/SOURCES.md). Known gaps: [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Refresh

Pass `--previous <earlier envelopes.jsonl>`. The run diffs each new envelope against the earlier one and writes
typed changes with materiality into `envelope.changes`. Previous snapshots stay where they are. A source that
fails during a refresh keeps the previous value in the change record. Running the same inputs against the same
previous file twice gives the same changes. See [docs/REFRESH.md](docs/REFRESH.md).

## The site

`python -m signalpost site` (or `python tools/build_site.py --envelopes ... --out site/`) renders a static site:

- `site/index.html`: searchable, filterable, sortable directory with a compare panel.
- `site/c/<org>.html`: the full envelope, with every value linked to its evidence, and an "Ask this profile" box
  that answers only from claims on the page.

The site opens from disk (`file://`). It loads nothing from the network. Stdlib only, no framework.

## Tests

```bash
uv run --extra test pytest -q
```

## Submission checklist

- [x] `uv.lock` committed and `uv sync --frozen` succeeds
- [ ] `./run.sh` completes a 100-company smoke test and a 1,000-company batch inside `SIGNALPOST_TIME_BUDGET_MIN`
- [x] `python -m signalpost validate` reports zero problems and exactly `--expected-count` envelopes (1,000)
- [x] a refresh run with `--previous` produces a `changes_by_type` block and leaves the earlier `--out` untouched
- [x] at least 1,000 completed profiles rendered and copied, with `manifest-1000.txt`, `envelopes-1000.jsonl.gz`, `run-report-1000.json`, `requests-1000.jsonl.gz` and the rendered `site/`, into `submission/` and committed (`out/` and the top-level `site/` are git-ignored)
- [x] no key is in the repository (`.env` is git-ignored), and the run works without one
- [x] `eval/report.json` regenerated on the frozen commit (see [docs/EVAL.md](docs/EVAL.md))
- [ ] email `submit@builderr.ai` in the current template: agent name, repository URL, exact commit hash, 100-company smoke-test result or report URL, the `run.sh` command, models / APIs / licences ("no models; Brave Search API for website discovery"), expected cost per official run (measured, see Budget), contact

## Documents

| Document | What it covers |
|---|---|
| [docs/AGENT.md](docs/AGENT.md) | research policy, the source ladder, when the agent abstains |
| [docs/CRAWLERS.md](docs/CRAWLERS.md) | every connector, its budget, its failure rules |
| [docs/IDENTITY_RESOLUTION.md](docs/IDENTITY_RESOLUTION.md) | website candidates and the exact-entity publication gate |
| [docs/DATA_SCHEMA.md](docs/DATA_SCHEMA.md) | envelope, claim, evidence, change, error and report schemas |
| [docs/REFRESH.md](docs/REFRESH.md) | snapshots, diffs, change types, idempotence |
| [docs/EVAL.md](docs/EVAL.md) | the dev split, the strategy registry, the promotion rule |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | known gaps, licences, restricted platforms |
| [docs/SOURCES.md](docs/SOURCES.md) | endpoints, rights and access rules per source |
| [SPEC.md](SPEC.md) | the internal build spec with the frozen module contracts |
