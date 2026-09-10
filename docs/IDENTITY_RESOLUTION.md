# Identity resolution: candidates and the publication gate

The registry proves who the company is. It does not prove which website is the company's. This document says how
website candidates are made, how a candidate is scored, and what is published at each score.

## Candidates (`signalpost/discovery.py`)

Ordered list. Each entry is `{"url", "origin", "note"}` with origin `registry`, `domain_guess` or `brave`.

1. **Registry website field.** The `hjemmeside` value from Enhetsregisteret, normalised to a scheme and host.
2. **Deterministic domain guesses from the legal name**, at most 4:
   - fold `ø→o`, `å→a`, `æ→ae`, lower-case, drop punctuation;
   - remove legal-form words (AS, ASA, ANS, DA, ENK, NUF, SA, BA, KS, and similar);
   - join the remaining tokens with `""` and with `-`;
   - try `.no` first, then `.com`.
   Example: `NORDIC SKOG AS` → `nordicskog.no`, `nordic-skog.no`, `nordicskog.com`, `nordic-skog.com`.
3. **Brave Search API**, only when `BRAVE_API_KEY` is set. One query per company. Results are transient: they order candidates and are never written to the envelope or the snapshots as evidence.

Never a candidate: proff.no, 1881.no, gulesider.no, purehelp.no and other directories; linkedin.com, facebook.com,
instagram.com and other platforms. Such hosts are dropped from every origin, including Brave results.

Candidates are probed in order. The first candidate that scores `exact` wins and probing stops. Each probe costs
requests (see CRAWLERS.md), so a company with four wrong guesses spends its budget on proving they are wrong.

## The gate (`signalpost/identity.py`)

`assess(profile, page)` returns a score, a status, the reasons, the supporting `claim_span`, and the public brand and aliases seen on the page.

| Test on the fetched homepage | Score | Status |
|---|---|---|
| the organisation number appears on the page, with or without spaces, with or without a `NO` prefix or `MVA` suffix | 1.0 | `exact` |
| every legal-name token (folded, legal-form stopwords removed) appears in the title, `og:site_name`, JSON-LD name or footer, **and** one corroborator matches the registry: postcode, street, municipality or phone | 0.95 | `exact` |
| every name token appears, but no corroborator | 0.8 | `review` |
| the page is parked or for sale | 0.1 | `related_or_uncertain` |
| anything else | below 0.8 | `related_or_uncertain` |

Only `exact` (score ≥ 0.9) is published. `review` is not published. The threshold is frozen for the submission.

## What is published at each status

| Status | `web` section | `official_website` claim | Site crawl | Other sections |
|---|---|---|---|---|
| `exact` | `available` | value = final URL, confidence = score, evidence = the homepage snapshot with the matching span | yes | site leaders, locations, jobs, news and social profiles may be added, each with its own evidence |
| `review` | `ambiguous` | `availability: "ambiguous"`, confidence 0.8, the candidate URL and the reason in the note; the URL is not presented as the company's site | no | nothing from the candidate page |
| `related_or_uncertain` | `ambiguous` (candidate seen) | `ambiguous`, reason in the note | no | nothing |
| no candidate at all | `not_available` | `not_available`, note "no website candidate" | no | nothing |
| candidate blocked by robots or 403 | `blocked` | `blocked` | no | nothing |

The `identity.official_website` header field is filled only from an `exact` result. The site's `public_brand` and
`aliases` are recorded only from an `exact` result.

## Related entities are not the entity

The gate rejects these by construction, and the `review` path keeps them out of the published profile:

- **Parent brand or group site** that lists the company as one of several: the group's organisation number is not the company's, and the company's own address is usually absent.
- **Franchise or chain site** shared by many legal entities: same reason.
- **Sister company** with a similar name: name tokens differ, or the corroborator (address, phone) points to the other entity.
- **Portfolio or "our companies" pages**: name tokens may match, but they are treated as `review` at best because the page belongs to another entity's domain.

Group, parent, subsidiary and brand relationships are labelled in notes where the registry states them. They are never collapsed into the company's own facts.

## Job ads: the NAV name gate (`signalpost/jobs.py`)

A NAV search result is accepted only when `_source.businessName` or `_source.employer.name`, folded, equals the legal
name exactly, or equals the legal name with its legal-form suffix removed. "Nordic Skog AS" and "Nordic Skog Holding AS"
do not match each other. Ads from sister companies are rejected.

## Social profiles

A social or video profile is published only when the verified site links to it (JSON-LD `sameAs` or an anchor on a
crawled page). The claim is `social_profile` with value `{platform, url}`, source class `company_owned`, evidence = the
page that links to it. The platform itself is never fetched, so followers, posts and headcounts are not claimed.

## Not implemented

The playbook's leader bridge (verified registry role → public profile on a permitted source → back to the entity) is
not implemented. It would need a permitted people-profile source, and none is used in this version.

## The domain-spells-the-name corroborator

A registered domain that spells out a **multi-word** legal name is treated as corroboration, on its own:
`afgruppen.no` for AF GRUPPEN ASA, `sandneselektriske.no` for SANDNES ELEKTRISKE AS. The company had to register
that domain, and a namesake collision on a full multi-word name is far less likely than on a single word.

The rule deliberately excludes single-word names. `vit.no` for VIT AS, `skard.no` for SKARD AS or `tgs.com` for
TGS ASA stay at `review` and are published as `ambiguous` with the candidate in the note, not as a verified
website. This costs recall on a handful of real sites and is the intended trade: the hard gates are 95% external
precision and no material wrong-company publication, and a one-word domain match is exactly the case where a
namesake would slip through.

Conditions, all required: at least two name tokens before generic-word stripping, a compacted name of at least
seven characters, and the first hostname label (after dropping `www`) equal to the compacted name.

## Job ads: the establishment, not the legal entity

A Norwegian job ad names the establishment that is hiring, so NAV's `employer.orgnr` is usually a subunit
(underenhet) number rather than the parent's. NORDICNEUROLAB AS is organisation number 891043082 and its NAV ad
carries 991095802, the subunit the official registry lists under it. Gating on the parent number alone silently
dropped those ads: a first 1,000-company run published zero jobs, which is impossible against roughly 9,600 active
national ads.

An ad is therefore accepted when its `employer.orgnr` equals the entity's own number **or** a subunit number
returned by the official subunit endpoint for that entity in the same run. The link is proved by the registry, not
by name similarity, so this is still exact-entity attribution; the claim records which subunit posted it. Sister
companies with near-identical names stay rejected: PREG BARNEHAGER AS matched four ads by name and published none,
because "Preg Barnehager Askøy AS" and its siblings are separate legal entities, not its subunits.

## A group's site is not the member company's site

blaauw.no carries the right brand and the exact registered address of BLAAUW AS, because the whole Blaauw group
sits at C. Sundtsgate 1, 5004 Bergen. Name and address cannot separate them. The page separates itself: it is
titled "Home | Einar Blaauw AS" and its text reads "For BLAAUW AS, our wholesale and trading company, click here",
pointing our company's real homepage somewhere else.

So a page is not a verified website when its own title (or OpenGraph name) introduces a **different registered
company** whose distinctive name tokens contain ours plus at least one more. A longer name that the registry
lists among our own former names is exempt, so "Sandnes Elektriske Forretning AS" never disqualifies
SANDNES ELEKTRISKE AS. A title naming us exactly settles the question in our favour and stops the check.

Measured over the 196 published and ambiguous companies of the 2026-09-10 run, this rule fires on one company
and changes no other verdict.
