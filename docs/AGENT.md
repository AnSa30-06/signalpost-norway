# Agent policy: research and abstention

This document says what the agent publishes, in what order it looks, and when it stops and says "not established".

## Principles

1. The organisation number is the only stable key. A name, a domain or a social handle is a candidate until it is tied back to that exact entity.
2. Every published value carries evidence: a stored snapshot, its hash, the retrieval time, and the literal text span that supports the value.
3. No LLM. Extraction, identity scoring, the diff and the summary text are fixed rules. The same inputs and the same snapshots give the same envelope.
4. No browser. Static HTTP only. A page that needs JavaScript to show its content yields what its static HTML holds, and nothing more.
5. Absence is a state. `not_available`, `blocked`, `not_applicable`, `ambiguous` and `failed` are written as such. Zero is written only when a source was checked and returned zero.
6. Abstention is reported, never hidden. A section the agent could not establish is listed in `synthesis.cannot_establish`, and its state is visible in the envelope and on the site.

## Source ladder

The agent follows the playbook ladder. Each rung is used only for what the rung can prove.

| Rung | Source | Used for | Evidence? |
|---|---|---|---|
| 1 | Brønnøysundregistrene APIs (entity, accounts, roles, subunits, updates) | identity anchor, financials, registered people and workplaces, dated registry events | yes |
| 2 | verified company-owned website (homepage, sitemap, targeted pages, RSS, JSON-LD, OpenGraph) | brand, description, contacts, site leaders, locations, jobs, news, linked social profiles | yes, after the identity gate |
| 3 | official platform API: NAV arbeidsplassen.no job search | job ads matched to the exact legal name | yes |
| 4 | other permitted public pages | not used in this version | – |
| 5 | Brave Search API (optional) | website candidates only | never |

Search rank is never evidence. A candidate from rung 5 becomes evidence only after the candidate page itself is fetched, stored and passed through the identity gate.

## Per-company flow

1. Seed from the universe row: organisation number, legal name, legal form, municipality, industry.
2. Fetch the five official endpoints. Emit `identity`, `accounts`, `leadership`, `workplaces` and `activity` claims. If the entity lookup itself fails, the envelope's terminal status is `failed` and the remaining steps still run where they can.
3. Build website candidates: registry `hjemmeside`, then deterministic domain guesses from the legal name (at most 4), then Brave if a key is set. Directory sites and social platforms are never candidates.
4. Fetch candidates in order (robots.txt first). Score each homepage with the identity gate (IDENTITY_RESOLUTION.md). Stop at the first `exact` result.
5. If a candidate is `exact`: crawl the site within its page budget and emit `web`, `identity` (public brand), `leadership`, `workplaces`, `hiring` and `activity` claims. Otherwise the `web` section is `ambiguous` (candidate seen, identity not proven) or `not_available` (no candidate), and nothing from any candidate page is published.
6. Run the NAV job search with the exact legal name. Accept ads only on an exact employer-name match.
7. Fetch the registry update feed for dated events.
8. Build the synthesis from `available` claims only.
9. If `--previous` was given, diff against the previous envelope and emit typed changes (REFRESH.md).
10. Validate the envelope (DATA_SCHEMA.md) and write it. There is always exactly one envelope per input.

## Extraction order

Structured data first, then attributes, then text: JSON-LD, then OpenGraph and meta tags, then DOM selectors
(footer, address blocks, anchors), then plain-text patterns (organisation number, phone, e-mail). Conflicting
candidates are kept with their own evidence rather than silently merged. The first layer that yields a value sets the claim; later layers may add corroboration.

## When the agent abstains

| Situation | What is written |
|---|---|
| homepage matches the name only, no organisation number and no address or phone corroboration | `official_website` = `ambiguous`, candidate URL in the note, confidence 0.8, nothing else from the site |
| parked or for-sale page | `ambiguous` with score 0.1, not published |
| robots.txt disallows the path, or robots.txt returns 401/403 | `blocked` |
| HTTP 401, 403 or 451 on a page | `blocked` |
| HTTP 404 or 410 | `not_available` |
| network error, timeout after one retry, parse failure | `failed`, plus an error record |
| per-company or global request cap reached | `not_available` with the note that the budget was exhausted; `operations.budget_exhausted` = `true` |
| NAV search succeeded but no ad matches the exact legal name | `active_job_count` = 0, `available`, note "checked NAV, no ads matched exact legal name" |
| NAV search failed | `active_job_count` = `failed` |
| legal form that never files annual accounts | `accounts` = `not_applicable` |
| registry has no employee count | `employees` = `not_available` |

Related entities are never published as the company: parent brands, franchises, sister companies, group portfolio pages
and "our companies" listings fail the gate by design.

## What the agent never does

- It never fetches LinkedIn, Meta, Glassdoor, Indeed or Google pages. A social profile is recorded only when the verified company site links to it, and the platform is not visited.
- It never follows a link off the verified site's registered domain.
- It never stores a search result as evidence.
- It never fills a missing financial value, or infers one from another year or another entity.
- It never converts a missing count to zero.
- It never fetches a URL that fails the public-URL check (SOURCES.md, "URL safety"), including redirect targets.

## Confidence

`confidence` on a claim is the strength of the entity match and the extraction, not a probability estimate.
Registry values are 1.0. Website claims carry the identity-gate score of the site they came from
(1.0 organisation number on page, 0.95 name plus corroboration). Values from JSON-LD or the registry API carry
their site or source score; values from text patterns may be lower and say so in the note.
