# Remediation after the first evaluation report (2026-09-12)

Builderr's provisional score for commit `78c4ee7`: **74.67/100** — coverage 25.67/35, accuracy 22/30,
updates 16/20, answers 6/10, usability 5/5. Qualification was **blocked** by two wrong-company publications.
The report named three faults. This document states the root cause of each, the fix, and how the fix was
verified. Every fix has a regression test.

## Fault 1 — two wrong-company publications (qualification blocker)

> "in two cases, it attached a different business's website and brand to the company being assessed"

Builderr's batch is not ours, so the two companies are unknown; the remediation therefore closes every route by
which the gate at `78c4ee7` could still publish a wrong site, rather than patching two examples.

### Routes that were open at `78c4ee7`

| Route | Why it was wrong | Closed by |
|---|---|---|
| Organisation number found **anywhere** on the page scored 1.0 immediately | A group site listing its subsidiaries, an auditor's client list, a supplier's "our customers" page, a tender notice or a directory all carry the number without being the company's site | R1: the number must be **self-identifying** — labelled, or beside the company's own name in an identity position — and a page carrying two or more *other* labelled numbers is a listing |
| Name tokens counted as present when scattered anywhere in body text | A page that mentions "Nordic" and "Bistro" in unrelated sentences, plus the word "Oslo", passed as NORDIC BISTRO AS | R2: name presence is split into **self-identification** (title, OpenGraph, JSON-LD, footer, h1/h2, copyright line, author) and **body mention**; only self-identification, or the full name as a contiguous phrase, can reach exact |
| A city name alone corroborated multi-word names | Every business in Oslo has "Oslo" on its page | R3: place-only corroboration never reaches exact; a postcode, a street, or a domain that spells a multi-word name is required |
| The full-name phrase counted wherever it appeared | A supplier's page saying "our customers include Norfrag Tank og Silo AS" would have verified the supplier's site as Norfrag's | R4: a page whose identity positions name a **different** company (any registered-company name with a legal-form suffix that shares no token with ours) is a third party's site, whatever the body says |
| No notion of a listing page | A page naming many companies is a directory or a group index | R5: three or more distinct registered-company names on a page whose title is not ours → `review` |
| `public_brand` fell back to any title when no token overlapped | The wrong site's title became the company's "brand" | R6: a brand is published only when it shares a token with the legal name; otherwise none |

### What "leave uncertain matches unconfirmed" now means in the output

Anything short of exact is published as `ambiguous` with the candidate URL in the note and the reason code in
`identity.website_identity.reasons`. No site fact — brand, social profile, contact, leader, news — is ever taken
from a non-exact page.

## Fault 2 — missed company-name changes

> "It also missed company-name changes"

Root cause: `refresh.diff` compared only list-valued fields (roles, jobs, locations, news) and financial fields.
The identity scalars — `legal_name`, `legal_form`, addresses, status flags, registered website, employee count,
industry — were never compared, so a renamed company produced no change record at all.

Fix (R7): every identity scalar is diffed. New change types: `changed_name`, `changed_legal_form`,
`changed_address`, `changed_status`, `changed_registry_website`, `changed_employee_count`, `changed_industry`.
`changed_name` also carries the registry's `former_names` list as corroboration when it lists the old name.

## Fault 3 — missed new filings when revenue was absent

> "some new financial filings when revenue was absent … Track name and filing-period changes separately from
> revenue, so a missing revenue value does not hide a new filing"

Root cause: `new_filing` was emitted only when the `reporting_period` attached to the **revenue** claim changed,
and only `available` claims were compared. 206 of the 1,000 sampled companies file accounts with no
`sumDriftsinntekter` (holding and property entities), so their revenue claim is `not_available` and a new filing
could never be detected.

Fix (R8): the reporting period is tracked as its own claim and diffed independently of every money field.
`new_filing` fires when the `reporting_period.to` date advances, when a new period appears in
`accounts_history`, or when `latest_submitted_accounts_year` increases — whichever is seen first; one filing
yields one change. `changed_financials` remains per field for the same period.

## Also improved in this revision

- **Filing history (R9):** the normalised accounts endpoint returns the latest period only (measured: one record
  for every company checked, including a listed ASA), so prior-year figures cannot come from it. The agent now
  reads the registry's filing-years endpoint (`/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar`, one
  paced request per company, about 30 a minute) and publishes `accounts_filing_years`, `first_filing_year` and
  `filings_on_file`. A new year in that list is a `new_filing` in its own right. Prior-year *figures* exist only as
  PDF copies and are not fetched; the code path for `accounts_prior_period` remains, and fires if the endpoint
  ever returns more than one period.
- **Synthesis (R10):** adds a financial trend sentence, risk flags (bankrupt / liquidating / forced liquidation),
  company age, and a plain statement of *how* the website was verified. A machine-readable `answers` block lists
  standard questions with the claim and evidence ids that answer them, and says explicitly when the evidence
  cannot answer.

## Verification

1. Regression tests for every rule (`tests/`).
2. Offline re-assessment of the fixed gate over every stored homepage snapshot from the previous submission run:
   every verdict that changed was read and recorded in `eval/gold.jsonl`.
3. A synthetic refresh test with a renamed company and a no-revenue filer with a new period.
4. A full 1,000-company run, validated, scored against the gold set, packaged in `submission/`.

Numbers from step 2 and 4 are in `docs/EVAL.md` under "Remediation audit".
