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
| `SIGNALPOST_WORKERS` | `8` | parallel companies |
| `BRAVE_API_KEY` | unset | enables Brave Search for website-candidate discovery |

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

Locked evaluator budget: 100 inputs, 45 minutes, 2,000 outbound requests, $10 declared third-party spend.

This agent runs 100 companies with at most 1,950 requests (`--max-requests 1950`) and at most 26 requests per
company (`--per-company-cap 26`). Companies with no website stop at about 11 requests, so the batch average stays near 9-15. Redirect hops and retries count. Cache hits inside one run are free.
When a company hits its cap, the remaining sources are marked `not_available` with a note, the envelope is still
written, and `operations.budget_exhausted` is `true`.

Third-party cost is $0 without Brave. With `BRAVE_API_KEY` set, the cost is about $0.50 per 100 companies
on Brave's paid plan (one search per company that has no registry website). The number is the plan's list price,
not a measurement. See [docs/CRAWLERS.md](docs/CRAWLERS.md) for the per-company request plan.

## Measured

First 1,000-company run, 2026-09-09, 32 workers, this laptop (`submission/run-report-1000.json`, `eval/report-run1.json`):

| Measure | Value |
|---|---|
| envelopes / inputs | 1,000 / 1,000, zero validation problems |
| requests | 8,572 total, 8.6 per company mean, 26 max (one company hit its cap) |
| wall clock | 10.7 min for 1,000 companies at 32 workers; p50 14 s, p95 54 s per company |
| bytes | 134 MB of snapshots |
| third-party cost | $0 |
| identity / accounts / leadership / workplaces / activity | 1,000 / 999 / 999 / 1,000 / 1,000 available |
| web | 101 verified exact, 103 ambiguous (name matched, not proven), 780 no site found, 10 failed, 6 blocked |
| hiring | 899 `failed`: NAV answered HTTP 429 for this IP during the run (see LIMITATIONS.md); 13 available, 88 not_available |
| evidence completeness | 100% of `available` claims carry evidence with a literal span |

A refresh pass against that run is the submitted artifact (`submission/`); its report is `submission/run-report-1000.json`
and its change counts are in `changes_by_type`. For a 100-company evaluator batch the same per-company numbers give
roughly 900-1,500 requests and 4-8 minutes at 8 workers.

## Secrets

Two optional environment variables. `NAV_FEED_TOKEN`: a private consumer token for NAV's job vacancy feed; without it
the agent fetches NAV's public experimentation token once per run. `BRAVE_API_KEY`: without it the agent never calls Brave.
Brave results are used only to propose website candidates. They are never stored as evidence.
No other key, token or account is used. Nothing is written outside `--out` and `--out/../site` (or the `--out` you give to `site`).

## Source rights

- Brønnøysundregistrene open data (Enhetsregisteret, Regnskapsregisteret, roles, subunits, oppdateringer): Norwegian Licence for Open Government Data (NLOD) 2.0.
- NAV job vacancy feed (pam-stilling-feed.nav.no): official API, bearer token, terms at arbeidsplassen.nav.no/vilkar-api (anyone may use it, free, republishing allowed, inactive ads must not be shown, contacts never published).
- Company-owned websites: `robots.txt` respected (fail closed on 401/403), identified user agent, 12 s timeout, 2 MB per page, about 10 pages per site, registered domain only.
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
- [ ] `./run.sh` completes on a 100-line batch within the budget (check `run-report.json`: `requests` ≤ 1950)
- [x] `python -m signalpost validate` reports zero problems and exactly `--expected-count` envelopes (1,000)
- [x] a refresh run with `--previous` produces a `changes_by_type` block and leaves the earlier `--out` untouched
- [x] at least 1,000 completed profiles rendered and copied, with `manifest-1000.txt`, `envelopes-1000.jsonl.gz`, `run-report-1000.json`, `requests-1000.jsonl.gz` and the rendered `site/`, into `submission/` and committed (`out/` and the top-level `site/` are git-ignored)
- [x] `BRAVE_API_KEY` is not in the repository, and the run works without it
- [x] `eval/report.json` regenerated on the frozen commit (see [docs/EVAL.md](docs/EVAL.md))
- [ ] email `submit@builderr.ai`: repository URL, exact commit hash, completed-profile count, manifest, the `run.sh` command, "no models; Brave Search API optional", expected cost per 100-company batch ($0, or about $0.50 with Brave), agent name and contact

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
