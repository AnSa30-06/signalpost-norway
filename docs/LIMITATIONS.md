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

When robots.txt returns 401 or 403 the host is treated as fully disallowed. When robots.txt is unreachable for another
reason (network error, 404), the fetch proceeds and the attempt is logged. This follows the common reading of the
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
