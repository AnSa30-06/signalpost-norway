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
3. **Evidence validity**: every `available` claim points to an existing snapshot whose bytes hash to `content_sha256` and contain `claim_span` (raw bytes for JSON and XML; visible text or markup for HTML; whitespace collapsed). `python eval/audit_artifact.py --run <dir>` checks this for every evidence record and exits non-zero on any failure.
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

## Hand-verified precision audit, 2026-09-10

Thirty published websites were checked by hand. Each page was fetched over a network path outside this machine's
inspected LAN and read for four things: our organisation number, the legal name, the registered street or postcode,
and whether the page is a different business, a parking page or a shared portal. The labels and the quoted evidence
are in `eval/gold.jsonl`.

Against the agent as it stood at commit 59bead23, six of the thirty were wrong:

| Company | Published | What it really was |
|---|---|---|
| PAULSEN DRIFT AS | paulsendrift.no | One.com placeholder, "Webhosting made simple" |
| HOA INVEST AS | hoainvest.com | a California investment firm |
| INDUSTRIFINANS AS | industrifinans.no | right brand and street, page states org number 993 075 558 |
| BUCK HOLDING AS | buckholding.com | bot-verification wall |
| UNIQ INVEST AS | uniqinvest.com | 114-byte empty document |
| ECHO HOLDING AS | echoholding.com | 114-byte empty document |

That is 24/30 = 80% exact-entity precision, against a 95% hard gate. All six share one root cause: the legal-name
tokens were matched against the **hostname**, and the same hostname was then counted a second time as
corroboration. One fact was doing two jobs, so a domain that merely spelled the company's name was enough to
publish a page that identified nobody.

Three rules were added in response, each with a regression test:

1. The name must appear in the page's **own content** (title, OpenGraph, JSON-LD, footer or body text). A hostname
   match alone now yields `name_partial`, never a verified website.
2. `domain_is_legal_name` counts as corroboration only when something ties the page to Norway: a `.no` host, a
   `+47` number, a `.no` address, or the country's name. A first attempt used "four digits then a capitalised
   word" as a Norwegian postcode test; it read the Californian street address "2300 Palm" as a postcode and had
   to be removed.
3. A page that states a **different** organisation number under an org-number label is never a verified website,
   however well the name and address match. This is what separates a sister company from the company.

Parking and hosting placeholders were also added to the parked-page markers, after five One.com "under
construction" pages and three domain-parking pages were found among the published set.

Re-running the fixed gate offline against the 136 stored homepage snapshots from run `2026-09-09-submission-1000-final`
rejects 14 of them and keeps 122. Every one of the 14 was inspected: eight parking or placeholder pages, two
different stated organisation numbers, three empty or bot-walled documents, and one name-only stub. No correct
site was lost. On the thirty hand-labelled rows the fixed gate publishes no known-wrong website.

The remaining honest gap: 40 of the 70 published sites in the sample were not hand-checked, so the measured
precision covers the audited rows only. Recall is not measured at all, because it needs the evaluator's pooled
union of what every entrant found.

## Second audit round, 2026-09-10: 200 companies

The first audit covered thirty companies and fixed six wrong publications. This round covered about two hundred,
chosen to sit where the risk actually is rather than at random:

- all 86 published sites verified by the 0.95 rule (name plus a corroborator) — the rule that can be wrong;
- a check of the 42 published by the 1.0 rule, which requires the organisation number on the page;
- all 66 companies the agent marked `ambiguous`, to measure the opposite error: withholding a correct site.

Each published page was read from its stored snapshot, and any page whose snapshot left the question open was
fetched again over a path outside this machine's network. Labels and quoted evidence are in `eval/gold.jsonl`.

**Precision.** Of the 86 risky publications, two were wrong:

| Company | Published | What it was |
|---|---|---|
| POCO LOCO AS | poco-loco.no | a bare LiteSpeed directory listing, "Index of /" |
| BLAAUW AS | blaauw.no | the group's site: "Einar Blaauw AS", contact "Blaauw Holding AS" |

One more looked wrong and was not. `varmefag.no` is a national fireplace chain, but the page **we stored** reads
"Velkommen til Varmefag Ålesund. Hos Varmefag Ålesund (PD Stafseth AS)" — the operator is named, so the match is
right. It is recorded as `correct_weak_url`, because the published address is the chain root and another visitor
would not see that store. The same category covers the housing co-op whose page lives on the OBOS portal.

**Recall.** Of the 66 `ambiguous` companies, five were correctly withheld (a French motorsport firm, a United
States hotel, the McCormick brand site, a California investment firm, and a page stating another entity's
organisation number) — but **seven were wrong to withhold**. In each the page simply writes the company's name
out in full, for example "Norfrag Tank og Silo as tilbyr leveranse av tanker og siloer", while carrying no
address to corroborate it.

Two rules came out of this round:

1. A bare web-server directory listing is a parked page, never a website.
2. The full legal name written as a **contiguous phrase** on a page with a Norway signal is corroboration in its
   own right. Separate tokens scattered over a page are weak; the whole name as a phrase is not something an
   unrelated site produces. Multi-word names only, so a one-word namesake cannot use it.

Re-running the fixed gate offline over the stored snapshots: 127 of 128 publications stand, POCO LOCO is dropped,
and 7 of the withheld companies are promoted. No foreign namesake was promoted — the Norway signal held every one.

**Still not measured.** Recall against the evaluator's pooled union of what every entrant found. That number
cannot be produced locally, and no figure here should be read as a substitute for it.

## Remediation audit, 2026-09-12 (after Builderr's first report)

Builderr scored commit `78c4ee7` at 74.67/100 and blocked qualification on two wrong-company publications in
their own 100-company batch. Their companies are not in our sample, so the gate was tightened on every route that
could still publish a wrong site (`docs/REMEDIATION.md`), and then re-measured offline over the stored homepage
snapshots of the previous submission run — 142 published sites and 57 withheld candidates.

| Outcome | Count | Detail |
|---|---|---|
| publications that stand | 138 of 142 | 79 name + registered address, 44 organisation number on page, 9 full legal name as a phrase, 6 via the registry's own e-mail domain among those |
| publications withdrawn | 4 | ikess.no (a group site: "Ikess består av Ikess kurs og Ikess Norge"); varmefag.no (a national chain's root page); erv.no (carries four other organisation numbers); ontime.no (a six-letter generic name, city corroboration only) |
| withheld candidates promoted | 1 | wilsonship.no, because the registry holds WILSON MANAGEMENT AS's e-mail address on that domain |

Every withdrawn site was read; each is a page that belongs to a group, a chain or a listing rather than to the
legal entity, which is exactly the class of error the report described. No publication that a reader would call
the company's own site was lost.

Refresh: five regression tests in `tests/test_refresh_remediation.py` cover a renamed company, a no-revenue filer
with a new period, address and status changes, one filing producing one record, and self-diff idempotence.

Full-run numbers for this revision are in `README.md` under "Measured" and in `submission/run-report-1000.json`.

### Full-run numbers for revision 2 (2026-09-13, `out/run-final5`, code commit `b1e87d7`)

1,000 of 1,000 envelopes, zero validation problems; 144 verified websites (92 name + registered address, 43
organisation number on page, 9 full legal name as a phrase), 108 ambiguous, 725 no site, 21 unreachable, 2
blocked; BLAAUW AS withheld; every one of the 1,000 companies carries its filing-year history; the `answers`
block is present on all 1,000 profiles. Gold: precision 1.000, recall 0.964, zero wrong-company publications on
the 46 labelled rows. Requests 10,218; 37.3 minutes at 24 workers, paced by the filing-years endpoint.

Evidence audit (`python eval/audit_artifact.py --run out/run-final5`): 44,099 evidence records checked, 0 spans
not verbatim in their snapshot; all 144 published websites re-checked from the stored homepage for another
organisation number, another entity in the title, a many-company listing and a foreign brand: 0 hits; the current
gate re-run on every stored homepage still says `exact` for all 144. The same audit over the previous packaging
(`out/run-final3`) had found 27,391 of 48,240 spans not verbatim, which is what led to the verbatim-span change
(docs/REMEDIATION.md, R11).

Refresh against the previous full run (`out/run-final4`, 40 minutes earlier, same code but for the two evidence
fixes): 3 `availability_changed` on `municipality` caused by the fix, 1 website unreachable this run (recorded as
`failed`, not removed), 1 `new_social_profile`; the other 143 verified websites identical. End-to-end refresh
scenarios through `signalpost run --previous` on real companies: a renamed company yields exactly one
`changed_name`; a revenue-absent filer whose period moved yields exactly one `new_filing` and no revenue change;
an unchanged company yields no change records.

### Judge-shaped run (2026-09-13, `out/judge-100`, the evaluator command itself)

`./run.sh work/judge-100.jsonl out/judge-100 out/run-final5/envelopes.jsonl` on a fixed random 100 of the 1,000
(seed 20261013), with the packaged envelopes as the previous day, `uv sync --frozen` included: 100 of 100
envelopes, zero validation problems; 1,180 requests (105 feed scan + 10.8 per company; three companies reached the
26-request cap, one of them with 24 used because a further website candidate needs three, and each says so in
`operations.budget_exhausted`); 570 s of agent time plus 58 s of `uv sync` at 8
workers; p50 11.6 s, p95 111.3 s; $0. Zero change records against the packaged output and identical section
states on all 100, which is the false-change rate the promotion rule requires. Evidence audit: 4,227 records, 0
non-verbatim spans, all 10 published websites clean.
