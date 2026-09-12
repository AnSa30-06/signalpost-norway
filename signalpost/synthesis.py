"""Deterministic English synthesis from claims only. Never states a value that is not in an available claim."""
from __future__ import annotations

from .models import AVAILABLE, NOT_APPLICABLE, SECTIONS

LEGAL_FORMS = {"AS": "private limited company (AS)", "ASA": "public limited company (ASA)",
               "ENK": "sole proprietorship (ENK)", "ANS": "general partnership (ANS)",
               "DA": "partnership with shared liability (DA)", "NUF": "branch of a foreign company (NUF)",
               "SA": "cooperative (SA)", "BA": "limited-liability company (BA)", "KS": "limited partnership (KS)",
               "STI": "foundation (STI)", "FLI": "association (FLI)", "IKS": "inter-municipal company (IKS)",
               "KF": "municipal enterprise (KF)", "SF": "state enterprise (SF)"}
ROLE_CODES = {"DAGL": "general manager", "LEDE": "board chair", "NEST": "deputy chair", "MEDL": "board member",
              "INNH": "proprietor", "KONT": "contact person", "REVI": "auditor", "REGN": "accountant"}
FIELD_NAMES = {"official_website": "official website", "active_job_count": "NAV job ads", "job_posting": "job ads",
               "registry_update": "registry update history", "revenue": "revenue figures",
               "accounts_history": "accounts history", "role": "registered roles", "workplace": "registered workplaces",
               "social_profile": "social media profiles", "contact_email": "contact email",
               "contact_phone": "contact phone", "news_item": "news items", "site_leader": "leadership named on the website",
               "site_location": "locations named on the website", "registry_employees": "employee count"}
STATE_PHRASES = {"not_available": "could be found", "blocked": "could be checked because the source blocked access",
                 "ambiguous": "could be confirmed", "failed": "could be retrieved because the fetch failed"}
CHANGE_PHRASES = {"new_job": ("new job ad", "new job ads"), "closed_job": ("job ad closed", "job ads closed"),
                  "new_role": ("new role", "new roles"), "removed_role": ("role removed", "roles removed"),
                  "new_filing": ("new annual filing", "new annual filings"),
                  "changed_financials": ("changed financial figure", "changed financial figures"),
                  "new_website": ("new website", "new websites"), "changed_website": ("changed website", "changed websites"),
                  "new_social_profile": ("new social profile", "new social profiles"),
                  "new_location": ("new location", "new locations"), "removed_location": ("location removed", "locations removed"),
                  "new_news": ("new news item", "new news items"), "changed_description": ("changed website description",) * 2,
                  "new_registry_update": ("new registry update", "new registry updates"),
                  "availability_changed": ("availability change", "availability changes")}
EMPTY = {"summary": "", "what_it_does": None, "size": None, "trend": None, "risk_flags": [], "leadership": None,
         "footprint": None, "hiring": None, "recent_activity": None, "what_changed": None, "verification": None,
         "answers": [], "cannot_establish": []}

IDENTITY_REASON_TEXT = {
    "org_number_on_page": "the company's organisation number is printed on the page",
    "name_and_address": "the page names the company and carries its registered address",
    "self_identified": "the page names the company in its title, heading, footer or structured data",
    "legal_name_phrase_on_page": "the page writes the company's full legal name out",
    "domain_and_address": "the domain spells the name and the registered address is on the page",
}


def _s(v):
    """Scalar -> clean string, else None."""
    if isinstance(v, (bool, dict, list, tuple, set)) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).strip()
    return s or None


def _num(v):
    try:
        return None if isinstance(v, bool) else float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def money(v) -> str | None:
    """Absolute NOK amount -> 'NOK 24.1 million' / 'NOK 1.23 billion' / 'NOK 850,000'."""
    n = _num(v)
    if n is None:
        return None
    n = abs(n)
    if n >= 1e9:
        s = f"{n / 1e9:.2f}".rstrip("0").rstrip(".") + " billion"
    elif n >= 1e6:
        s = f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + " million"
    else:
        s = f"{n:,.0f}"
    return f"NOK {s}"


def _amount(label: str, v, loss_label: str | None = None) -> str | None:
    m = money(v)
    if m is None:
        return None
    if (_num(v) or 0) < 0:
        return f"a loss of {m}" if loss_label is None else f"{loss_label} {m}"
    return f"{label} {m}"


def _place(v):
    s = _s(v)
    return s.title() if s and s.isupper() else s


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _dot(s: str) -> str:
    return s if s.endswith((".", "!", "?")) else s + "."


def build(envelope: dict) -> dict:
    try:
        return _build(envelope if isinstance(envelope, dict) else {})
    except Exception as exc:  # defensive: synthesis must never break the envelope
        return {**EMPTY, "summary": f"Synthesis unavailable ({type(exc).__name__})."}


def _build(env: dict) -> dict:
    claims = [c for c in env.get("claims") or [] if isinstance(c, dict)]
    by: dict[str, list] = {}
    for c in claims:
        if c.get("availability") == AVAILABLE:
            by.setdefault(str(c.get("field")), []).append(c)

    def first(field):
        return by[field][0].get("value") if by.get(field) else None

    def dicts(field):
        return [c.get("value") for c in by.get(field, []) if isinstance(c.get("value"), dict)]

    ident = env.get("identity") if isinstance(env.get("identity"), dict) else {}
    name = _s(first("legal_name")) or _s(ident.get("legal_name")) or f"Organisation {_s(env.get('organisation_number')) or 'unknown'}"
    brand = _s(first("public_brand"))

    # --- what it does -------------------------------------------------------------------------------------
    form_raw = first("legal_form")
    form = _s(form_raw.get("code")) if isinstance(form_raw, dict) else _s(form_raw)
    form_desc = LEGAL_FORMS.get(form.upper(), f"organisation of legal form {form}") if form else "organisation"
    where = _place(first("municipality"))
    code, label = _s(first("industry_code")), _s(first("industry_label"))
    s1 = f"{name} is a Norwegian {form_desc}" + (f" in {where}" if where else "")
    if code or label:
        s1 += " registered under industry " + " ".join(x for x in (code, label) if x)
    s1 = _dot(s1)
    if brand and brand.casefold() != name.casefold():
        s1 += f" It trades as {brand}."
    what = s1
    desc = _s(first("website_description"))
    if desc:
        what += f" Its website describes it as: “{desc[:200].rstrip('.')}”."

    # --- size ------------------------------------------------------------------------------------------------
    size_parts = []
    emp = _num(first("registry_employees"))
    if emp is not None:
        emp = int(emp)
        size_parts.append("The registry reports no employees." if emp == 0 else
                          f"The registry reports {emp} employee{'s' if emp != 1 else ''}.")
    rev_claim = by["revenue"][0] if by.get("revenue") else None
    period = _s(rev_claim.get("reporting_period")) if rev_claim else _s(first("reporting_period"))
    money_bits = [_amount("revenue of", first("revenue"), "negative revenue of"),
                  _amount("an annual result of", first("annual_result"))]
    money_bits = [b for b in money_bits if b]
    acc_sentence = None
    if money_bits:
        acc_sentence = (f"For the {period} accounting year it filed " if period else "Its latest accounts show ") + _join(money_bits) + "."
        size_parts.append(acc_sentence)
    extra = [_amount("an operating result of", first("operating_result"), "an operating loss of"),
             _amount("total assets of", first("total_assets")), _amount("equity of", first("equity"), "negative equity of"),
             _amount("total debt of", first("total_debt"))]
    extra = [b for b in extra if b]
    if extra:
        size_parts.append("It reported " + _join(extra) + ".")
    size = " ".join(size_parts) or None

    # --- trend (R10): latest period against the newest prior period ------------------------------------------
    trend = None
    priors = sorted([p for p in dicts("accounts_prior_period") if _s(p.get("reporting_period"))],
                    key=lambda p: _s(p["reporting_period"]), reverse=True)
    if priors:
        prev = priors[0]
        prev_year = _s(prev["reporting_period"]).split("..")[-1][:4]
        cur_year = (period or "").split("..")[-1][:4]
        trend_bits = []
        for fld, label in (("revenue", "Revenue"), ("annual_result", "The annual result"), ("equity", "Equity")):
            cur, old_v = _num(first(fld)), _num(prev.get(fld))
            if cur is None or old_v is None:
                continue
            if old_v == 0:
                direction = "was zero in the prior year and is now"
            elif fld == "annual_result" and (cur < 0) != (old_v < 0):
                direction = "moved from a loss to a profit," if cur > 0 else "moved from a profit to a loss,"
            else:
                pct = (cur - old_v) / abs(old_v) * 100
                direction = (f"rose {pct:.0f}% from" if pct >= 0.5 else f"fell {abs(pct):.0f}% from" if pct <= -0.5 else "was flat at") \
                    if abs(old_v) > 0 else "is"
            if direction.endswith("from"):
                trend_bits.append(f"{label} {direction} {money(old_v)} ({prev_year}) to {money(cur)} ({cur_year})")
            elif direction.startswith("moved"):
                trend_bits.append(f"{label} {direction} {money(old_v)} ({prev_year}) to {money(cur)} ({cur_year})")
            elif direction == "was flat at":
                trend_bits.append(f"{label} was flat at about {money(cur)} between {prev_year} and {cur_year}")
            else:
                trend_bits.append(f"{label} {direction} {money(cur)} ({cur_year})")
        if trend_bits:
            trend = _join(trend_bits) + "."
        elif len(priors) >= 1:
            trend = f"Accounts are on file for {len(priors) + 1} periods; the latest is {period}."
    years = first("accounts_filing_years")
    history = None
    if isinstance(years, list) and years:
        ys = sorted(str(y) for y in years)
        history = (f"Annual accounts are on file for {len(ys)} year{'s' if len(ys) != 1 else ''}, {ys[0]} to {ys[-1]}."
                   if len(ys) > 1 else f"One annual-account filing is on file, for {ys[0]}.")
        if not trend:
            trend = history + " The registry's normalised endpoint publishes figures for the latest period only, so no year-on-year comparison is made."

    # --- risk flags and age ------------------------------------------------------------------------------------
    risk_flags = []
    flags = first("status_flags")
    if isinstance(flags, dict):
        if flags.get("bankrupt"):
            risk_flags.append("The registry marks the company as bankrupt.")
        if flags.get("liquidating"):
            risk_flags.append("The registry marks the company as being wound up (under avvikling).")
        if flags.get("forced_liquidation"):
            risk_flags.append("The registry marks the company as under forced liquidation or dissolution.")
    eq = _num(first("equity"))
    if eq is not None and eq < 0:
        risk_flags.append(f"Equity is negative ({money(eq)}) in the latest filed accounts.")
    founded = _s(first("founded")) or _s(first("registration_date"))
    if founded and founded[:4].isdigit():
        age = 2026 - int(founded[:4])
        size = ((size + " ") if size else "") + f"It was founded in {founded[:4]}, so it is about {age} year{'s' if age != 1 else ''} old."

    # --- leadership ----------------------------------------------------------------------------------------
    by_role: dict[str, list[str]] = {}
    for r in dicts("role"):
        person = _s(r.get("name"))
        code_r = _s(r.get("role_code"))
        label_r = ROLE_CODES.get(code_r.upper()) if code_r else None
        label_r = label_r or (_s(r.get("role")) or "").lower() or None
        if person and label_r:
            by_role.setdefault(label_r, []).append(person)
    lead_parts = []
    heads = [f"the {k} is {by_role[k][0]}" for k in ("general manager", "board chair", "proprietor") if by_role.get(k)]
    if heads:
        sentence = _dot("; ".join(heads))
        lead_parts.append(sentence[0].upper() + sentence[1:])
    lead_summary = lead_parts[0] if lead_parts else None
    for k, people in by_role.items():
        if k in ("general manager", "board chair", "proprietor"):
            continue
        plural = k + "s" if not k.endswith("s") else k
        lead_parts.append(f"{plural.capitalize() if len(people) > 1 else k.capitalize()}: {_join(people[:6])}.")
    site_leads = [f"{_s(l.get('name'))} ({_s(l.get('title'))})" if _s(l.get("title")) else _s(l.get("name"))
                  for l in dicts("site_leader") if _s(l.get("name"))]
    if site_leads:
        lead_parts.append(f"The website names {_join(site_leads[:4])}.")
    leadership = " ".join(lead_parts) or None

    # --- footprint -------------------------------------------------------------------------------------------
    foot = []
    for fld, lab in (("business_address", "Business address"), ("registered_address", "Registered address")):
        a = first(fld)
        a = ", ".join(_s(x) for x in a if _s(x)) if isinstance(a, list) else (_s(a.get("address") if isinstance(a, dict) else a))
        if a:
            foot.append(f"{lab}: {a}.")
            break
    wps = [w for w in dicts("workplace") if _s(w.get("name"))]
    if wps:
        named = [(_s(w["name"]) + (f" ({_place(w.get('address'))})" if _s(w.get("address")) else "")) for w in wps[:4]]
        foot.append(f"The registry lists {len(wps)} sub-unit{'s' if len(wps) != 1 else ''}: {_join(named)}"
                    + (" and others." if len(wps) > 4 else "."))
    locs = [_s(l.get("name")) or _s(l.get("address")) for l in dicts("site_location")]
    locs = [l for l in locs if l]
    if locs:
        foot.append(f"The website names {_join(locs[:5])} as location{'s' if len(locs) != 1 else ''}.")
    footprint = " ".join(foot) or None

    # --- hiring ----------------------------------------------------------------------------------------------
    hire = []
    count = _num(first("active_job_count"))
    count_sentence = None
    if count is not None:
        count = int(count)
        count_sentence = "It has no active job ads on NAV." if count == 0 else \
            f"It has {count} active job ad{'s' if count != 1 else ''} on NAV."
        hire.append(count_sentence)
    ads = [j for j in dicts("job_posting") if _s(j.get("title"))]
    if ads:
        shown = [f"{_s(j['title'])}" + (f" ({_place(j.get('location'))})" if _s(j.get("location")) else "") for j in ads[:3]]
        hire.append(f"Open roles include {_join(shown)}.")
    careers = _s(first("careers_page"))
    if careers:
        hire.append(f"Careers page: {careers}.")
    hiring = " ".join(hire) or None

    # --- recent activity -----------------------------------------------------------------------------------------
    dated = []
    for n in dicts("news_item"):
        if _s(n.get("date")):
            dated.append((_s(n["date"])[:10], f"news item “{_s(n.get('title')) or 'untitled'}”"))
    updates = [u for u in dicts("registry_update") if _s(u.get("date"))]
    for u in updates:
        dated.append((_s(u["date"])[:10], f"registry update ({_s(u.get('change_type')) or 'unknown type'})"))
    for j in dicts("job_posting"):
        if _s(j.get("date_posted")):
            dated.append((_s(j["date_posted"])[:10], "job ad posted"))
    lastmod = _s(first("sitemap_lastmod"))
    if lastmod:
        dated.append((lastmod[:10], "sitemap update"))
    dated.sort(reverse=True)
    latest = f"Latest dated activity: {dated[0][1]} on {dated[0][0]}." if dated else None
    act = [latest] if latest else []
    if updates:
        act.append(f"The registry recorded {len(updates)} update event{'s' if len(updates) != 1 else ''}, "
                   f"the most recent on {max(_s(u['date'])[:10] for u in updates)}.")
    recent_activity = " ".join(act) or None

    # --- what changed ----------------------------------------------------------------------------------------
    run = env.get("run") if isinstance(env.get("run"), dict) else {}
    refresh = run.get("refresh") if isinstance(run.get("refresh"), dict) else {}
    changes = [c for c in env.get("changes") or [] if isinstance(c, dict)]
    if refresh.get("baseline"):
        what_changed = "First run — no previous snapshot."
    elif not changes:
        what_changed = "No changes since the previous run."
    else:
        counts: dict[str, int] = {}
        for c in changes:
            counts[str(c.get("change_type"))] = counts.get(str(c.get("change_type")), 0) + 1
        bits = []
        for t, n in counts.items():
            sing, plur = CHANGE_PHRASES.get(t, (t.replace("_", " "), t.replace("_", " ") + " changes"))
            bits.append(f"{n} {sing if n == 1 else plur}")
        what_changed = "Since the previous run: " + ", ".join(bits) + "."

    # --- website + summary ---------------------------------------------------------------------------------------
    site = _s(first("official_website"))
    site_sentence = None
    if site:
        note = _s(by["official_website"][0].get("note"))
        site_sentence = f"Its verified website is {site}" + (f" ({note.rstrip('.')})" if note else "") + "."
    summary = " ".join(x for x in (s1, size_parts[0] if emp is not None else None, acc_sentence, lead_summary,
                                   site_sentence, count_sentence, latest) if x)

    # --- cannot establish ----------------------------------------------------------------------------------------
    cannot, seen = [], set()
    for c in sorted(claims, key=lambda c: SECTIONS.index(c.get("section")) if c.get("section") in SECTIONS else 99):
        st = c.get("availability")
        if st in (AVAILABLE, NOT_APPLICABLE) or st not in STATE_PHRASES or by.get(str(c.get("field"))):
            continue
        key = (c.get("field"), st)
        if key in seen:
            continue
        seen.add(key)
        human = FIELD_NAMES.get(str(c.get("field")), str(c.get("field") or "value").replace("_", " "))
        note = _s(c.get("note"))
        cannot.append(f"No {human} {STATE_PHRASES[st]}" + (f" ({note.rstrip('.')})" if note else "") + ".")
    covered = {c.get("section") for c in claims}
    sections = env.get("sections") if isinstance(env.get("sections"), dict) else {}
    for sec, st in sections.items():
        if st in STATE_PHRASES and sec not in covered:
            cannot.append(f"No {sec} information {STATE_PHRASES[st]}.")

    # --- how the website was verified (R10) --------------------------------------------------------------------
    verification = None
    wi = ident.get("website_identity") if isinstance(ident.get("website_identity"), dict) else {}
    reasons = [str(r) for r in (wi.get("reasons") or [])]
    if site and reasons:
        texts = [IDENTITY_REASON_TEXT[r] for r in reasons if r in IDENTITY_REASON_TEXT]
        extras = [r for r in reasons if r.startswith(("postcode:", "street:", "domain_is_legal_name:", "registry_email_domain:"))]
        human_extras = []
        for r in extras:
            k, _, v = r.partition(":")
            human_extras.append({"postcode": f"registered postcode {v}", "street": f"registered street {v}",
                                 "domain_is_legal_name": f"the domain {v} spells the legal name",
                                 "registry_email_domain": f"the e-mail domain the company filed with the registry, {v}"}[k])
        verification = "Website verified because " + _join(texts[:2] + human_extras[:2]) + "." if (texts or human_extras) else None
    elif not site:
        ow = next((c for c in claims if c.get("field") == "official_website"), None)
        if ow and ow.get("availability") == "ambiguous" and _s(ow.get("value")):
            verification = f"A candidate website ({_s(ow['value'])}) matched the name but could not be tied to this exact legal entity, so it is not published as verified."

    # --- answers (R10): standard questions, answered only from available claims, with the claim ids -----------
    def _claim_ids(field):
        return [str(c.get("id")) for c in by.get(field, []) if c.get("id")]

    def _ev_ids(field):
        out = []
        for c in by.get(field, []):
            out.extend(str(x) for x in (c.get("evidence_ids") or []))
        return out

    answers = []

    def answer(q, text, fields):
        ok = bool(text)
        answers.append({"question": q, "answerable": ok,
                        "answer": text if ok else "The evidence does not establish this.",
                        "claim_ids": [i for f in fields for i in _claim_ids(f)] if ok else [],
                        "evidence_ids": sorted(set(i for f in fields for i in _ev_ids(f))) if ok else []})

    answer("What does the company do?", what, ["legal_name", "industry_code", "industry_label", "website_description"])
    answer("Who leads it?", lead_summary, ["role"])
    answer("How big is it?", (size_parts[0] if emp is not None else None) or acc_sentence, ["registry_employees", "revenue", "annual_result"])
    answer("What did its latest accounts show?", acc_sentence, ["revenue", "annual_result", "reporting_period"])
    answer("Is it growing?", _join(trend_bits) + "." if priors and trend_bits else None, ["accounts_prior_period", "revenue", "annual_result"])
    answer("How long has it filed accounts?", history, ["accounts_filing_years", "first_filing_year", "filings_on_file"])
    answer("Where is it?", footprint, ["business_address", "workplace", "site_location"])
    answer("Is it hiring?", count_sentence or (hire[0] if hire else None), ["active_job_count", "job_posting"])
    answer("What is its website?", site, ["official_website"])
    answer("What has happened recently?", latest, ["news_item", "registry_update", "job_posting", "sitemap_lastmod"])
    answer("What changed since the last run?", None if refresh.get("baseline") else what_changed, [])
    answer("Are there warning signs?", " ".join(risk_flags) if risk_flags else ("No bankruptcy, winding-up or forced-liquidation flag is set in the registry." if isinstance(flags, dict) else None), ["status_flags", "equity"])
    if refresh.get("baseline"):
        answers[-2]["answer"] = "First run — there is no previous snapshot to compare."

    summary = " ".join(x for x in (summary, trend, " ".join(risk_flags) if risk_flags else None) if x)
    return {"summary": summary, "what_it_does": what, "size": size, "trend": trend, "risk_flags": risk_flags,
            "leadership": leadership, "footprint": footprint, "hiring": hiring, "recent_activity": recent_activity,
            "what_changed": what_changed, "verification": verification, "answers": answers, "cannot_establish": cannot}
