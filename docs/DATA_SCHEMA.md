# Data schema

One envelope per input organisation number. All records are plain JSON. `signalpost/models.py` validates every
envelope with pydantic before it is written, and `python -m signalpost validate` runs the same check again.

`schema_version` is `"1.0"`.

## Envelope

```json
{"organisation_number":"938702675","schema_version":"1.0",
 "run":{"run_id":"run-001","started_at":"2026-09-09T10:00:00Z","completed_at":"2026-09-09T10:00:09Z",
        "terminal_status":"completed","agent_version":"<git sha>","previous_run_id":null,
        "refresh":{"baseline":true,"previous_snapshot":null}},
 "identity":{"legal_name":"...","legal_form":"AS","municipality":"OSLO","industry_code":"41.000",
             "industry_label":"...","public_brand":null,"official_website":null},
 "sections":{"identity":"available","accounts":"available","leadership":"available","workplaces":"available",
             "web":"ambiguous","hiring":"available","activity":"available"},
 "claims":[],"evidence":[],"changes":[],"errors":[],
 "operations":{"requests":17,"runtime_ms":8120,"third_party_cost_usd":0,"bytes":123456,"budget_exhausted":false},
 "synthesis":{}}
```

`terminal_status`:

- `completed`: no section is `failed`.
- `completed_with_errors`: at least one section is `failed`.
- `failed`: the registry anchor itself (Enhetsregisteret lookup) failed. The envelope is still written.

`identity` is the header block. `official_website` in it is set only when the web gate returned `exact`.
`public_brand` is set only when the verified site names a brand.

## Sections and availability states

Sections, exact strings: `identity`, `accounts`, `leadership`, `workplaces`, `web`, `hiring`, `activity`.

States, exact strings:

| State | Meaning |
|---|---|
| `available` | checked, found, supported by evidence |
| `not_available` | checked, nothing exists (for example, zero subunits, zero matching job ads) |
| `blocked` | robots.txt, HTTP 401/403/451, or a platform policy stopped the fetch |
| `not_applicable` | the source does not apply to this entity (for example, accounts for a form that never files) |
| `ambiguous` | a candidate exists but exact identity was not proven; the candidate is not published as fact |
| `failed` | network or parse failure |

A section state is the roll-up of its claims. When a field was checked and nothing exists, the pipeline emits
one claim with `value: null`, `availability: "not_available"` and a `note`. Zero is never written in place of absence.

## Claim

```json
{"id":"c-12","section":"web","field":"official_website","value":"https://example.no/",
 "availability":"available","confidence":0.99,"evidence_ids":["ev-3"],
 "reporting_period":null,"effective_date":null,"note":null}
```

- `id` is unique inside the envelope. Prefixes tell the producer apart: `c-` registry and pipeline, `site-` site crawl,
  `job-` NAV, `upd-` registry updates.
- `value` may be a string, number, object or `null`. Object values are used for structured fields:
  `social_profile` `{platform,url}`, `site_leader` `{name,title}`, `site_location` `{name,address}`,
  `job_posting` `{title,url,date_posted,valid_through,location,source}`, `news_item` `{title,url,date}`,
  `registry_update` `{date,change_type}`.
- `confidence` is 0 to 1. Registry values are 1.0. Website identity confidence is the gate score (see IDENTITY_RESOLUTION.md).
- `reporting_period` is set on financial claims (for example `"2025"`). `effective_date` is set on dated events.
- An `available` claim must have at least one evidence id. The validator rejects an envelope where it does not.

## Evidence

```json
{"id":"ev-3","source_url":"https://example.no/om-oss","final_url":"https://www.example.no/om-oss/",
 "source_class":"company_owned","retrieved_at":"2026-09-09T12:00:04Z","http_status":200,
 "content_sha256":"<hex>","snapshot_path":"snapshots/<hex>.html","claim_span":"Org.nr. 987 654 321",
 "extraction_method":"footer_regex","reporting_period":null}
```

- `source_url` is the URL that was requested. `final_url` is where the request ended after redirects.
- `content_sha256` is the hash of the raw bytes. `snapshot_path` is relative to the run directory and holds those bytes.
- `claim_span` is verbatim text from the snapshot, at most 300 characters, whitespace collapsed. For JSON and XML sources it is an excerpt of the raw bytes. For HTML pages it is an excerpt of the visible text, or of the markup for a value read from an attribute, a `<meta>` tag or JSON-LD (kept in the escaped form the page uses). It is always set. Two computed counts carry a description of the computation instead of an excerpt: `active_job_count` from the NAV feed scan (`nav_feed_scan`) and `role_count`. `eval/audit_artifact.py --run <dir>` checks every evidence record of a run against its snapshot.
- `extraction_method` names the extractor (for example `json_ld`, `og`, `footer_regex`, `api_field`).

Source classes, exact strings: `official_registry`, `official_accounts`, `official_roles`, `official_subunits`,
`official_updates`, `company_owned`, `official_job_board`, `search_candidate`.
`search_candidate` marks candidate-only material. It is never referenced by a published claim.

## Change

```json
{"field":"job_posting","change_type":"new_job","materiality":"material","previous_value":null,
 "current_value":{"title":"...","url":"..."},"first_observed":"2026-09-09T10:00:00Z",
 "last_observed":"2026-09-09T10:00:00Z","evidence_ids":["evj-1"],"previous_evidence_ids":[]}
```

Change types: `new_website`, `changed_website`, `new_social_profile`, `new_role`, `removed_role`, `new_filing`,
`changed_financials`, `new_job`, `closed_job`, `new_location`, `removed_location`, `new_news`,
`changed_description`, `new_registry_update`, `availability_changed`. Materiality is `material` or `minor`.
See REFRESH.md for the stable claim keys and the materiality table.

## Error

```json
{"stage":"jobs","source_url":"https://arbeidsplassen.nav.no/...","message":"network: timed out","availability":"failed"}
```

`stage` names the connector. `message` is at most 300 characters. `availability` is the state the error produced.

## Operations

`requests` counts every attempt for this company, including redirect hops and retries. `bytes` is the sum of
response bodies. `third_party_cost_usd` is 0 unless Brave was called. `budget_exhausted` is `true` when the
per-company cap or the global cap stopped a fetch.

## Synthesis

```json
{"summary":"...","what_it_does":null,"size":"...","leadership":"...","footprint":null,"hiring":"...",
 "recent_activity":"...","what_changed":null,"cannot_establish":["official website (ambiguous)"]}
```

Every sentence is built from `available` claims by fixed templates. `cannot_establish` lists every section or
field that is `not_available`, `blocked`, `ambiguous` or `failed`, in plain words.

## Run-level files

`run-report.json`:

```json
{"run_id":"run-001","started_at":"...","completed_at":"...","inputs":100,"envelopes":100,
 "terminal_status_counts":{"completed":97,"completed_with_errors":3},
 "section_state_counts":{"web":{"available":61,"ambiguous":30,"not_available":9}},
 "requests":1412,"bytes":52000000,"third_party_cost_usd":0,"runtime_ms":900000,"p50_ms":7000,"p95_ms":20000,
 "budget_exhausted_companies":[],"previous_run_id":null,"changes_by_type":{}}
```

The numbers above show the shape only. They are not measurements.

`requests.jsonl`: one row per HTTP attempt with `company`, `url`, `final_url`, `status`, `attempts`, `redirects`,
`error`, `sha256`, `retrieved_at`, `elapsed_ms`.

`manifest.txt`: the input organisation numbers, one per line, in input order.

`checkpoint.jsonl`: envelopes already finished, used to resume an interrupted run.

## Validation rules

`python -m signalpost validate --envelopes <file> --expected-count N` fails when:

- the line count is not N, or an organisation number is not nine digits;
- a claim's section or availability, or an evidence source class, is not one of the exact strings above;
- an `available` claim has no evidence ids, or references an evidence id that does not exist;
- a section state is not one of the six states.

## Additions on 2026-09-12

- Claims `accounts_filing_years` (list of years with an annual-account copy on file), `first_filing_year` and
  `filings_on_file` (section `accounts`), from the registry's filing-years endpoint, one evidence span each.
- Claim `accounts_prior_period` (section `accounts`): one per earlier filing returned by the normalised accounts
  endpoint. In practice that endpoint returns the latest period only, so this claim is rarely present; the
  filing history is `accounts_filing_years`.
- `synthesis.trend` — latest period against the newest prior period, in words.
- `synthesis.risk_flags` — list of plain sentences for bankruptcy, winding-up, forced liquidation, negative equity.
- `synthesis.verification` — one sentence saying *why* the website counts as verified, or why a candidate does not.
- `synthesis.answers` — list of `{question, answerable, answer, claim_ids, evidence_ids}` for eleven standard
  questions; `answerable: false` carries the fixed text "The evidence does not establish this."
- `identity.website_identity.reasons` may now include `self_identified`, `org_number:labelled`,
  `org_number:bare_digits`, `registry_email_domain:<domain>`, `host_carries_name_token`, and the cap codes listed
  in `docs/IDENTITY_RESOLUTION.md`.
- New change types in `changes[]`: see `docs/REFRESH.md`.
