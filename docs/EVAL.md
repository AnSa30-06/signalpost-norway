# Evaluation: corpus split, metrics, strategy registry and the promotion rule

This is the process the project follows. It is the playbook's control loop, run with fixed rules and no model.
Numbers are never written into this document by hand. Where a number belongs, the line says
"measured on <date>" and points at the report file the number comes from.

## Corpus

`eval/` holds the frozen development split and the scoring script. Three splits, with no organisation number and no
website host shared between them:

| Split | Purpose | Rule |
|---|---|---|
| development | tune extraction rules and candidate order | the only split anyone looks at while changing code |
| validation | set thresholds once (identity gate score, NAV name rule, page priorities) | used once per candidate release |
| final | run once, report, do not tune | the score reported before freezing |

Companies in each split are hand-labelled for:

- exact identity of every website candidate (`exact`, `review`, `related_or_uncertain`), with the reason;
- claim support: whether each published value is supported by its `claim_span` in its snapshot;
- expected availability per section (for example, a form that never files → `accounts` = `not_applicable`).

The daily 100-company evaluation batch is the hidden exam. Its scores and labels are never used to tune the running submission.

## Metrics, in the order they are judged

1. **Wrong-company publications**: count of published claims whose evidence belongs to a different legal entity. Must be zero.
2. **Supported-claim precision**: published `available` claims whose span supports the value, over all published claims.
3. **Evidence validity**: every `available` claim points to an existing snapshot whose bytes hash to `content_sha256` and contain `claim_span`.
4. **Coverage and recall** per field family (website, social profile, leaders, locations, jobs, news), as company recall and claim recall.
5. **Refresh correctness**: real changes found, and the false-change rate on a re-run against the same previous file (must be zero).
6. **Runtime, requests and cost**: p50 and p95 per company, requests per company, third-party cost per 100 companies.

Abstention (a section left `ambiguous`, `not_available`, `blocked` or `failed`) is reported as its own count. It is not coverage.

Current values: measured on 2026-09-09 on the frozen 1,000-company batch: `eval/report-run1.json` (first run) and `eval/report.json` (refresh pass against it). Gold labels for wrong-company and span-support checks are not yet hand-written, so those two rows are reported as "not measured" rather than guessed.

## Strategy registry

Every discovery or extraction route has a stable name and a version. A route enters production only through this table.

| Strategy | Version | In production | Notes |
|---|---|---|---|
| `registry_site` | 1 | yes | registry `hjemmeside` as first candidate |
| `domain_guess` | 1 | yes | at most 4 guesses, `.no` then `.com` |
| `brave_candidates` | 1 | optional (`BRAVE_API_KEY`) | candidates only, never evidence |
| `robots_sitemap` | 1 | yes | sitemap from robots.txt or `/sitemap.xml` |
| `static_homepage` | 1 | yes | identity gate on the static homepage |
| `targeted_paths` | 1 | yes | about, contact, team, careers, news, press |
| `jsonld_og` | 1 | yes | JSON-LD, then OpenGraph, then DOM, then text |
| `nav_jobs` | 1 | yes | exact legal-name match |
| `brreg_updates` | 1 | yes | dated registry events |
| `leader_bridge` | – | no | not implemented; needs a permitted people-profile source |
| `browser_fallback` | – | no | not implemented; no browser in this version |
| `pdf_accounts` | – | no | not implemented; JSON accounts API only |

Every attempt is preserved: the run directory keeps `requests.jsonl` (every URL, status, redirect count, hash),
the snapshots, and the envelope with accepted claims, rejected candidates (in notes and errors) and per-company
requests, runtime and cost. A failed strategy can therefore be told apart from a wrong-company strategy or an expensive one.

## Promotion rule

A challenger version replaces the production version only when all of these hold on the development split, and then once on validation:

1. zero new material wrong-company publications;
2. no meaningful drop in supported-claim precision (the declared tolerance is written in `eval/report.json` next to the measurement);
3. evidence completeness stays at 100% for published material claims;
4. useful coverage or recall improves by the declared minimum;
5. runtime, request count and cost stay inside the locked budget (100 companies, 45 minutes, 1,950 requests, $10).

The previous version stays in the registry for rollback. The decision and the exact report are stored with the version.

Routing is a decision table, not a learned policy: static HTML first; no browser; no PDF. A learned router is out of scope until enough labelled attempts exist.

## Frozen before each daily batch

- code and `uv.lock`;
- strategy versions in the table above;
- thresholds: identity gate publishes at score ≥ 0.9; NAV exact-name rule; per-company cap 22; global cap 1,950; page budget about 10 per site;
- the source allowlist in SOURCES.md.

Operational retries (one retry on 429/5xx or timeout) follow the frozen policy and are counted.

## Running the loop

```bash
uv run --extra test pytest -q          # unit and connector regression tests
```

The scoring script and its exact arguments are documented in `eval/` by the pipeline owner. Its output is `eval/report.json`.
Last full loop: 2026-09-09, first run then refresh pass; `python eval/score.py --envelopes out/<run>/envelopes.jsonl --report eval/report.json`.
