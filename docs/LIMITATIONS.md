# Limitations

Known gaps, by design or by budget, and the source restrictions that cause them. Shares and counts are not written here
by hand; where one belongs, see `eval/report.json`.

## Restricted platforms are not crawled

LinkedIn, Meta (Facebook, Instagram), Glassdoor, Indeed and Google are not fetched. Consequences:

- No LinkedIn headcount, follower count or employee list. The only headcount is the registry's registered employee count, which is self-reported and may be missing.
- No Glassdoor or Google ratings and no review sentiment. Company-owned copy cannot supply a sentiment claim either.
- No Indeed or LinkedIn job ads. Jobs come from NAV arbeidsplassen.no and from the verified company site. finn.no is a commercial site and is not crawled.
- A social profile is recorded only when the verified company site links to it. A company with a LinkedIn page that its own site does not link to has no `social_profile` claim.

These field families therefore score low or zero for this agent under the pooled-evidence union. That is a deliberate trade for a zero wrong-company rate and clean source rights.

## The identity gate is strict

A homepage that shows the company name but neither the organisation number nor a registry address or phone stays `review`
and is not published. Many small-company sites fall into this bucket, and a site that is really the company's is then
reported as `ambiguous`. The share of companies left ambiguous: see `eval/report.json`.

Domain guesses are limited to 4 and to `.no` and `.com`. A company whose brand differs from its legal name is found
only through the registry website field or through Brave. Without `BRAVE_API_KEY`, such companies usually end as `not_available` or `ambiguous` on the web section.

## No browser

Sites that render their content with JavaScript yield only what the static HTML carries (usually the title, meta tags and JSON-LD).
Leaders, locations and jobs on such sites are missed. There is no rendered fallback in this version.

## Budget

At most 22 requests per company. A candidate on a new host costs two requests (robots.txt plus the page), and redirects count.
A company with several wrong candidates, or a large site, is crawled partially: targeted pages that were not reached
are reported as `not_available` with a budget note, and `operations.budget_exhausted` is set.

## Financials

Financial values come from the Regnskapsregisteret JSON API only. Annual-account PDF copies are not parsed, so history
is limited to what the API returns. Entities whose legal form never files, or that have not filed for the latest year,
get `not_applicable` or `not_available`. No value is ever estimated or carried over from another year.

## robots.txt that cannot be fetched

When robots.txt cannot be fetched (401, 403, 404 or a network error) it is treated as unavailable, which under
RFC 9309 §2.3.1.3 means no restrictions; the page fetch then decides, and a refusal there is recorded as `blocked`.
Until 2026-09-13 a 401/403 robots.txt was read as "Disallow: /", stricter than the standard; that refused 17 hosts
on one 1,000-company run. A redirect onto another host is checked against that host's robots.txt before it is followed.
robots convention, but it is a policy choice and it is recorded per host in `requests.jsonl`.

## Refresh

Diffs rely on stable keys. A job ad or news item whose URL changes appears as one `closed_job`/`new_job` pair rather than
as a moved item. A site that changes domains appears as `changed_website`, and the previous site's derived claims may
appear as removed. Materiality is a fixed table, not a judgement.

## Synthesis

The summary is template text over `available` claims. It states what the claims state and nothing else. It does not
weigh, compare or judge, and it does not use an LLM.

## Language and names

Token folding handles æ, ø and å. Names in other scripts are matched as-is. The legal-form stopword list is Norwegian.
The NAV gate compares folded employer names exactly; an employer whose NAV name carries a department suffix is rejected.

## Site rendering

The static site renders each envelope as it is. Directory columns (employees, revenue, annual result, active jobs)
read the first `available` claim with the expected field name; a section that uses another field name shows a dash.
The "Ask this profile" box is a keyword router over the claims on the page, not a question-answering model; a question
it cannot map to a field gets "The evidence does not establish this".

## Licences and rights

- Brønnøysundregistrene data: NLOD 2.0. Attribution is carried by every evidence record's `source_url`.
- NAV arbeidsplassen.no: public government site; robots.txt allows all; listed on data.norge.no as public access.
- Company websites: fetched with an identified user agent, within robots.txt, at low volume. Raw snapshots are kept as private evidence for verification. The published site shows at most 300 characters of a page (`claim_span`), never a page copy.
- People: leader names come from the official roles register and from the company's own pages. No other personal data is collected, and no third-party people profiles are fetched.
- Brave Search API: used under Brave's API terms, for candidates only; results are not stored.

## NAV rate limiting (measured 2026-09-09)

`arbeidsplassen.nav.no` answered HTTP 429 to every request from the build machine for more than ninety minutes after
three small back-to-back smoke runs. The session therefore paces that host at one request per 3 s with concurrency 1,
retries a 429 twice with 3 s and 6 s waits, and after three consecutive 429s stops contacting the host for 90 s.
During a cooldown the hiring section is `failed` with the note "rate_limited_cooldown"; nothing is invented and the
previous run's job claims are kept in the change record. The submitted 1,000-profile artifact was produced while that
block was active, so most of its `hiring` sections are `failed`. A 100-company batch at one NAV request per 3 s takes
five minutes of NAV time and stays far below the burst that triggered the block.

## TLS on the build machine (measured 2026-09-09)

The 1,000-company artifact in `submission/` was produced on a network that inspects HTTPS. Reading the
certificate actually served showed the cause precisely: sites such as `vit.no` and `arktisk.no` were presented
with a certificate re-signed by a **Fortinet** inspection appliance (issuer `organizationName=Fortinet`,
`commonName=F2K60FTK23900226`), whose root is not installed on the machine. Those fetches cannot verify, and the
agent records them as `tls_intercepted` rather than a plain network error, so the reader can tell a bad network
from a bad site. They will not occur on a normal evaluator host. Set `SIGNALPOST_CA_BUNDLE` to a PEM file to add
such a root where it is legitimately installed.

A second, unrelated class was a real bug and is fixed. A valid site (`outdoor.no`, a current Let's Encrypt
certificate) failed with `unable to get local issuer certificate` against Python's platform trust store, and
verified against `certifi` on the same machine in the same second. The agent now uses the `certifi` bundle by
default, so the failure does not depend on how complete the host's trust store happens to be.

Three TLS states are now distinguished in a claim note: `tls_intercepted` (the network re-signed the certificate),
`tls_incomplete_chain` (the site did not send its intermediate), `tls_hostname_mismatch` (the certificate does not
cover the host, for example `galea.no` serving a certificate for `netzire.com`). Only the last is the company's
own fault, and none of the three is ever published as a verified website.

DNS on this machine also failed intermittently under 32 concurrent workers, including for hosts that plainly
exist. `assert_public_url` now retries a temporary resolver failure once and caches only stable results, so a
resolver hiccup is not recorded as "this domain does not exist".

## Known limits after revision 2 (2026-09-12)

- **A same-country namesake without a stated organisation number** cannot be separated from the company by any
  page content: two Norwegian firms called Nordic Bistro AS, each on its own site, each naming itself, only one
  carrying its number. The gate then depends on the registered postcode or street being on the page. Where
  neither is, the site is `ambiguous`, which costs recall rather than precision.
- **A group domain that the registry itself ties to the subsidiary** is accepted: WILSON MANAGEMENT AS filed an
  e-mail address on wilsonship.no, so that domain is published for it with the reason
  `registry_email_domain:wilsonship.no`. A reader who wants only the subsidiary's own pages should treat that
  reason code as a group signal.
- **The published URL is the page that proved identity**, which for a platform-hosted page can be the platform
  root: vibbo.no for a housing co-op, varmefag.no for a chain store. After revision 2 the chain root is no longer
  published (the page does not name itself as the operator); the co-op page still is, because it carries the
  co-op's own organisation number.
- **Recall against the evaluator's pooled union is not measurable locally.** Every coverage figure in this
  repository is coverage of what this agent found, not of what exists.

## What "blocked" and "unreachable" were, measured (2026-09-13)

On the 1,000-company run of 2026-09-12, 395 website candidate probes failed. Each class was checked:

| Class | Probes | Finding |
|---|---|---|
| timeout or connection refused | 291 | 40 re-probed from a clean network with a 15 s timeout: none answered. Dead servers and parked names. |
| robots.txt with `Disallow` | 51 | the host's stated wish; respected |
| robots.txt answering 401/403 | 17 | the agent's earlier rule read this as "disallow everything", stricter than RFC 9309. Fixed. Re-run, every one of the 17 then refused the page itself with 403/401 — bot walls on namesake domains — so the recorded state is now the site's own answer. |
| page answering 403 | 13 | our user agent and a browser user agent both receive 403: a WAF or JavaScript challenge, not agent-string blocking. Not bypassed. |
| TLS hostname mismatch or incomplete chain | 6 | server misconfiguration; the agent does not fetch missing intermediates |

None of the 395 is a site the agent could have read by behaving differently and honestly. Each is recorded with
its cause in `identity.website_candidates_probed` and in the `official_website` claim note.
