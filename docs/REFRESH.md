# Refresh: snapshots, diffs and idempotence

A refresh is a normal run with `--previous <earlier envelopes.jsonl>`. It re-fetches every source with the same
frozen strategy, writes a new envelope, and adds typed changes computed against the earlier envelope for the same organisation number.

```bash
python -m signalpost run --input batch.jsonl --universe universe.jsonl.gz \
  --out out/run-002 --run-id run-002 --previous out/run-001/envelopes.jsonl --expected-count 100
```

or `./run.sh batch.jsonl out/run-002 out/run-001/envelopes.jsonl`.

## Snapshots are never overwritten

- Each run writes into its own `--out` directory. The previous run's directory is read, never written.
- Snapshot files are named by content hash. A refresh that receives the same bytes writes nothing new; different bytes get a new file. Nothing is deleted.
- Evidence in the new envelope points at the new run's snapshots. Evidence ids from the previous envelope are kept in each change record as `previous_evidence_ids`, so both sides of a change stay verifiable.

## Refresh metadata in the envelope

```json
"run": {"run_id": "run-002", "previous_run_id": "run-001",
        "refresh": {"baseline": false, "previous_snapshot": "out/run-001/envelopes.jsonl"}}
```

A run without `--previous`, or a company that is not in the previous file, is a baseline: `baseline: true`, `changes: []`.

## The diff (`signalpost/refresh.py`)

`diff(previous_envelope, current_envelope)` is a pure function of the two envelopes. It uses a stable claim key:

```
(field, normalised value key)
```

where the value key is the part of the value that identifies the thing: the URL for websites, jobs and news items;
the name (and role) for people; the platform for social profiles; the reporting period for financial values;
the name or address for locations. Values are compared after whitespace and case folding.

Change record:

```json
{"field":"job_posting","change_type":"new_job","materiality":"material",
 "previous_value":null,"current_value":{"title":"...","url":"..."},
 "first_observed":"2026-09-09T10:00:00Z","last_observed":"2026-09-16T10:00:00Z",
 "evidence_ids":["evj-1"],"previous_evidence_ids":[]}
```

`first_observed` is when the current value was first seen (carried forward from the previous record when the value did not change);
`last_observed` is the current run.

## Change types

| Type | Emitted when |
|---|---|
| `new_website` | `official_website` becomes `available` and there was none before |
| `changed_website` | the verified website URL differs from the previous one |
| `new_social_profile` | a `social_profile` platform appears that was not linked before |
| `new_role` / `removed_role` | a registered or site-named person-role pair appears or disappears |
| `new_filing` | a financial claim for a reporting period that did not exist before |
| `changed_financials` | a financial value for the same reporting period differs |
| `new_job` / `closed_job` | a job posting URL appears or disappears |
| `new_location` / `removed_location` | a subunit or site location appears or disappears |
| `new_news` | a news item URL appears |
| `changed_description` | the website description text differs |
| `new_registry_update` | a registry update event with a new date appears |
| `availability_changed` | a field's availability state differs from the previous run (for example `available` → `blocked`) |

Materiality is `material` or `minor`. The intended rule: changes to identity, money, people, places and hiring are
`material` (website, filing, financials, roles, jobs, locations, and availability changes on those fields); new dated
activity and wording changes are `minor` (social profile, news, description, registry update). `signalpost/refresh.py` is the authoritative assignment.

## Failed refresh keeps the last supported value

When a source fails during a refresh (network, block, budget), the new claim carries its failure state and the
change record for that field keeps the previous value:

```json
{"field":"official_website","change_type":"availability_changed","materiality":"material",
 "previous_value":"https://example.no/","current_value":null,
 "previous_evidence_ids":["evs-1"],"evidence_ids":[]}
```

The previous value is not copied into the new claim as if it had been observed again. The reader sees both the last
supported value and the fact that this run could not confirm it.

## Idempotence

Running the same inputs against the same `--previous` file twice yields the same set of change records with the same
keys, types and materiality (timestamps differ only in `last_observed`, which is the run time). The diff has no
randomness and no hidden state. Snapshots are content-addressed, so identical responses map to identical paths.

## Cadence

The agent has no scheduler. The operator decides which previous file to diff against. The daily evaluation run is a
full refresh of 100 companies against the previous day. For a larger corpus, the playbook's volatility rule applies:
official accounts change yearly; roles and subunits change rarely; jobs, news and registry updates change often.
All connectors run on every refresh in this version; there is no per-source skip.

## Change types added after the first evaluation report (2026-09-12)

Builderr's report said the agent "missed company-name changes and some new financial filings when revenue was
absent". Both were true, and both are fixed here (see `docs/REMEDIATION.md`, R7 and R8).

| Change type | Fires when | Materiality |
|---|---|---|
| `changed_name` | `legal_name` differs from the previous run; also when the registry's `former_names` gains an entry and the current name is unchanged | material |
| `changed_legal_form` | the registered legal form differs | material |
| `changed_address` | the business or registered address differs | material |
| `changed_status` | a bankruptcy, winding-up or forced-liquidation flag flips | material |
| `changed_registry_website` | the `hjemmeside` field in the registry differs | material |
| `changed_industry` | the primary industry code differs | material |
| `changed_employee_count` | the registered employee count differs | minor |
| `changed_brand` | the public brand read from the verified site differs | minor |

**Filings are detected from the period, not from a money field.** `new_filing` now fires when any of
`reporting_period`, `accounts_history`, `accounts_filing_years` or `latest_submitted_accounts_year` advances, whichever is compared first;
one filing produces exactly one record. Before this change the trigger lived on the `revenue` claim, and only
`available` claims were compared, so the 206 sampled companies that file accounts with no revenue line
(holding and property entities) could never produce a `new_filing`. `changed_financials` is unchanged: one record
per money field whose value differs within the same period.

Every rule has a regression test in `tests/test_refresh_remediation.py`, including one that diffs an envelope
against an exact copy of itself and asserts an empty result.
