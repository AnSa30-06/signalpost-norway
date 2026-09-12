"""Official Brønnøysundregistrene modules: identity, annual accounts, roles, subunits.

Every claim carries evidence pointing at the exact API response snapshot. A 404 is an explicit
``not_available`` state, never a zero. Birth dates of role holders are never stored.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

from .models import (AMBIGUOUS, AVAILABLE, FAILED, NOT_APPLICABLE, NOT_AVAILABLE, IdGen, new_claim, new_error,
                     new_evidence, state_from_fetch)

BRREG = "https://data.brreg.no"
UNIVERSE_URL = "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz"
UNIVERSE_SHA = "b82d6a3e7231d1759a958c282bc4366b80ec2fab8095053d8ed7fa9cd01bc838"
# Legal forms that are not obliged to file annual accounts (sole proprietorships etc.). The universe is a
# 2025-filer universe, so every entity in it has filed; this only matters for inputs outside the universe.
NON_FILING_FORMS = {"ENK", "PERS", "UTLA", "PK", "KIRK"}
# The filing-years endpoint is rate-limited to about 30 requests a minute (measured by the starter kit). One
# process-wide pacer keeps every worker under that, without slowing the other registry endpoints.
HISTORY_INTERVAL = 2.1
_HISTORY_LOCK = threading.Lock()
_HISTORY_NEXT = [0.0]


def _history_slot() -> None:
    with _HISTORY_LOCK:
        now = time.monotonic()
        wait = _HISTORY_NEXT[0] - now
        _HISTORY_NEXT[0] = max(now, _HISTORY_NEXT[0]) + HISTORY_INTERVAL
    if wait > 0:
        time.sleep(wait)


def _addr(a: Optional[dict]) -> Optional[dict]:
    if not a:
        return None
    return {"street": " ".join(a.get("adresse") or []) or None, "postcode": a.get("postnummer"),
            "city": a.get("poststed"), "municipality": a.get("kommune"), "country": a.get("landkode") or a.get("land")}


def _span(obj, limit: int = 300) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))[:limit]


class Official:
    """Fetches and normalises the official modules for one organisation."""

    def __init__(self, session, ids: IdGen, org: str, universe_row: Optional[dict]):
        self.s = session
        self.ids = ids
        self.org = org
        self.row = universe_row or {}
        self.claims: list[dict] = []
        self.evidence: list[dict] = []
        self.errors: list[dict] = []
        self.sections: dict[str, str] = {}
        self.entity: Optional[dict] = None

    # ---- identity ---------------------------------------------------------------------------------------
    def identity(self) -> None:
        r = self.s.get(f"{BRREG}/enhetsregisteret/api/enheter/{self.org}", company=self.org, kind="json", robots=False)
        ent = r.json() if r.ok else None
        if ent and ent.get("organisasjonsnummer") == self.org:
            self.entity = ent
            ev = lambda span, method="brreg_enheter_api": self._ev(r, "official_registry", span, method)  # noqa: E731
            add = lambda field, value, span, **kw: self._add("identity", field, value, [ev(span)], **kw)  # noqa: E731
            add("legal_name", ent.get("navn"), _span({"navn": ent.get("navn")}))
            form = ent.get("organisasjonsform") or {}
            add("legal_form", {"code": form.get("kode"), "label": form.get("beskrivelse")}, _span(form))
            add("business_address", _addr(ent.get("forretningsadresse")), _span(ent.get("forretningsadresse")))
            if ent.get("postadresse"):
                add("registered_address", _addr(ent.get("postadresse")), _span(ent.get("postadresse")))
            ba = ent.get("forretningsadresse") or ent.get("postadresse") or {}
            add("municipality", ba.get("kommune") or self.row.get("municipality"), _span({"kommune": ba.get("kommune"), "kommunenummer": ba.get("kommunenummer")}))
            n1 = ent.get("naeringskode1") or {}
            add("industry_code", n1.get("kode"), _span(n1))
            add("industry_label", n1.get("beskrivelse"), _span(n1))
            if ent.get("harRegistrertAntallAnsatte"):
                add("registry_employees", ent.get("antallAnsatte"), _span({"antallAnsatte": ent.get("antallAnsatte")}))
            else:
                self._add("identity", "registry_employees", None, [ev(_span({"harRegistrertAntallAnsatte": ent.get("harRegistrertAntallAnsatte", False)}))],
                          availability=NOT_AVAILABLE, note="registry has no registered employee count for this entity")
            add("registration_date", ent.get("registreringsdatoEnhetsregisteret"), _span({"registreringsdatoEnhetsregisteret": ent.get("registreringsdatoEnhetsregisteret")}),
                effective_date=ent.get("registreringsdatoEnhetsregisteret"))
            if ent.get("stiftelsesdato"):
                add("founded", ent.get("stiftelsesdato"), _span({"stiftelsesdato": ent.get("stiftelsesdato")}), effective_date=ent.get("stiftelsesdato"))
            add("vat_registered", bool(ent.get("registrertIMvaregisteret")), _span({"registrertIMvaregisteret": ent.get("registrertIMvaregisteret")}))
            flags = {"bankrupt": bool(ent.get("konkurs")), "liquidating": bool(ent.get("underAvvikling")),
                     "forced_liquidation": bool(ent.get("underTvangsavviklingEllerTvangsopplosning"))}
            add("status_flags", flags, _span({k: ent.get(k) for k in ("konkurs", "underAvvikling", "underTvangsavviklingEllerTvangsopplosning")}))
            if ent.get("sisteInnsendteAarsregnskap"):
                add("latest_submitted_accounts_year", ent.get("sisteInnsendteAarsregnskap"), _span({"sisteInnsendteAarsregnskap": ent.get("sisteInnsendteAarsregnskap")}))
            if ent.get("vedtektsfestetFormaal"):
                add("stated_purpose", " ".join(ent["vedtektsfestetFormaal"])[:500], _span({"vedtektsfestetFormaal": ent["vedtektsfestetFormaal"]}))
            if ent.get("hjemmeside"):
                add("registry_website", ent["hjemmeside"], _span({"hjemmeside": ent["hjemmeside"]}), confidence=0.9)
            else:
                self._add("identity", "registry_website", None, [ev(_span({"hjemmeside": None}))], availability=NOT_AVAILABLE,
                          note="the registry lists no website for this entity")
            for src, field in (("epostadresse", "registry_email"), ("telefon", "registry_phone"), ("mobil", "registry_mobile")):
                if ent.get(src):
                    add(field, ent[src], _span({src: ent[src]}))
            if ent.get("historiskeNavn"):
                add("former_names", [h.get("navn") for h in ent["historiskeNavn"] if h.get("navn")], _span(ent["historiskeNavn"]))
            self.sections["identity"] = AVAILABLE
            return
        # Fallback: the frozen public universe row (derived from the official bulk snapshot).
        if self.row:
            fake = type("U", (), {})()
            fake.url = UNIVERSE_URL
            fake.final_url = UNIVERSE_URL
            fake.retrieved_at = r.retrieved_at
            fake.status = None
            fake.sha256 = UNIVERSE_SHA
            fake.snapshot_path = None
            ev = self._ev(fake, "official_registry", _span(self.row), "universe_row_fallback")
            note = f"live registry unavailable ({r.error or r.status}); values from the frozen 2025 universe snapshot"
            for field, key in (("legal_name", "name"), ("municipality", "municipality"), ("industry_code", "industry_code"),
                               ("industry_label", "industry_label"), ("registry_employees", "employees")):
                v = self.row.get(key)
                self._add("identity", field, v, [ev], availability=AVAILABLE if v not in (None, "") else NOT_AVAILABLE, note=note, confidence=0.9)
            self._add("identity", "legal_form", {"code": self.row.get("legal_form"), "label": None}, [ev], note=note, confidence=0.9)
            self._add("identity", "status_flags", {"bankrupt": bool(self.row.get("bankrupt")), "liquidating": bool(self.row.get("liquidating")), "forced_liquidation": None}, [ev], note=note, confidence=0.9)
            self._add("identity", "registry_website", self.row.get("website") or None, [ev], availability=AVAILABLE if self.row.get("website") else NOT_AVAILABLE, note=note, confidence=0.8)
            self.sections["identity"] = AVAILABLE
            self.errors.append(new_error("registry_live", r.error or f"http_{r.status}", state_from_fetch(r), r.url))
            return
        st = state_from_fetch(r)
        self.sections["identity"] = st
        self.errors.append(new_error("registry_live", r.error or f"http_{r.status}", st, r.url))
        self._add("identity", "legal_name", None, [], availability=st, note="registry lookup failed and the organisation number is not in the frozen universe")

    # ---- accounts ---------------------------------------------------------------------------------------
    def accounts(self) -> None:
        form = ((self.entity or {}).get("organisasjonsform") or {}).get("kode") or self.row.get("legal_form")
        r = self.s.get(f"{BRREG}/regnskapsregisteret/regnskap/{self.org}", company=self.org, kind="json", robots=False)
        data = r.json() if r.ok else None
        if not r.ok or not isinstance(data, list) or not data:
            if r.status == 404 and form in NON_FILING_FORMS:
                st, note = NOT_APPLICABLE, f"legal form {form} is not obliged to file annual accounts"
            elif r.status == 404 or (r.ok and not data):
                st, note = NOT_AVAILABLE, "Regnskapsregisteret returned no normalised annual accounts for this organisation"
            else:
                st, note = state_from_fetch(r), f"accounts endpoint failed: {r.error or r.status}"
                self.errors.append(new_error("accounts", r.error or f"http_{r.status}", st, r.url))
            ev = [self._ev(r, "official_accounts", f"HTTP {r.status}", "brreg_regnskap_api")] if r.status else []
            self._add("accounts", "revenue", None, ev, availability=st, note=note)
            self.sections["accounts"] = st
            return
        records = sorted(data, key=lambda x: (x.get("regnskapsperiode") or {}).get("tilDato") or "", reverse=True)
        latest = records[0]
        per = latest.get("regnskapsperiode") or {}
        period = f"{per.get('fraDato')}..{per.get('tilDato')}"
        res = latest.get("resultatregnskapResultat") or {}
        drift = res.get("driftsresultat") or {}
        ek = latest.get("egenkapitalGjeld") or {}
        eiend = latest.get("eiendeler") or {}
        values = {
            "revenue": ((drift.get("driftsinntekter") or {}).get("sumDriftsinntekter")),
            "operating_result": drift.get("driftsresultat"),
            "profit_before_tax": res.get("ordinaertResultatFoerSkattekostnad"),
            "annual_result": res.get("aarsresultat"),
            "total_assets": eiend.get("sumEiendeler"),
            "equity": (ek.get("egenkapital") or {}).get("sumEgenkapital"),
            "total_debt": (ek.get("gjeldOversikt") or {}).get("sumGjeld"),
        }
        paths = {"revenue": "resultatregnskapResultat.driftsresultat.driftsinntekter.sumDriftsinntekter",
                 "operating_result": "resultatregnskapResultat.driftsresultat.driftsresultat",
                 "profit_before_tax": "resultatregnskapResultat.ordinaertResultatFoerSkattekostnad",
                 "annual_result": "resultatregnskapResultat.aarsresultat", "total_assets": "eiendeler.sumEiendeler",
                 "equity": "egenkapitalGjeld.egenkapital.sumEgenkapital", "total_debt": "egenkapitalGjeld.gjeldOversikt.sumGjeld"}
        for field, val in values.items():
            span = _span({"regnskapsperiode": per, paths[field]: val})
            ev = self._ev(r, "official_accounts", span, "brreg_regnskap_api", reporting_period=period)
            if val is None:
                self._add("accounts", field, None, [ev], availability=NOT_AVAILABLE, reporting_period=period,
                          note="field absent from the filed normalised accounts (not zero)")
            else:
                self._add("accounts", field, float(val), [ev], reporting_period=period, effective_date=per.get("tilDato"))
        meta_span = _span({"regnskapsperiode": per, "valuta": latest.get("valuta"), "regnskapstype": latest.get("regnskapstype"),
                           "oppstillingsplan": latest.get("oppstillingsplan"), "revisjon": latest.get("revisjon")})
        ev = self._ev(r, "official_accounts", meta_span, "brreg_regnskap_api", reporting_period=period)
        self._add("accounts", "currency", latest.get("valuta") or "NOK", [ev], reporting_period=period)
        self._add("accounts", "reporting_period", {"from": per.get("fraDato"), "to": per.get("tilDato")}, [ev], reporting_period=period, effective_date=per.get("tilDato"))
        self._add("accounts", "accounts_type", {"type": latest.get("regnskapstype"), "layout": latest.get("oppstillingsplan"),
                  "small_company": (latest.get("regnkapsprinsipper") or {}).get("smaaForetak"), "consolidated_parent": (latest.get("virksomhet") or {}).get("morselskap"),
                  "audited": not (latest.get("revisjon") or {}).get("ikkeRevidertAarsregnskap", False)}, [ev], reporting_period=period)
        # R9: prior periods, one claim each. The API returns several filings; publishing only the latest hid the
        # history and left the synthesis without a trend. Each carries its own reporting period and evidence span.
        for prior in records[1:4]:
            pp = prior.get("regnskapsperiode") or {}
            pperiod = f"{pp.get('fraDato')}..{pp.get('tilDato')}"
            pres = prior.get("resultatregnskapResultat") or {}
            pdrift = pres.get("driftsresultat") or {}
            pek = prior.get("egenkapitalGjeld") or {}
            pval = {"reporting_period": pperiod,
                    "revenue": (pdrift.get("driftsinntekter") or {}).get("sumDriftsinntekter"),
                    "operating_result": pdrift.get("driftsresultat"),
                    "annual_result": pres.get("aarsresultat"),
                    "total_assets": (prior.get("eiendeler") or {}).get("sumEiendeler"),
                    "equity": (pek.get("egenkapital") or {}).get("sumEgenkapital"),
                    "total_debt": (pek.get("gjeldOversikt") or {}).get("sumGjeld"),
                    "currency": prior.get("valuta") or "NOK"}
            pval = {k: (float(v) if isinstance(v, (int, float)) and k not in ("reporting_period", "currency") else v) for k, v in pval.items()}
            pspan = _span({"regnskapsperiode": pp, "aarsresultat": pres.get("aarsresultat"),
                           "sumDriftsinntekter": (pdrift.get("driftsinntekter") or {}).get("sumDriftsinntekter")})
            self._add("accounts", "accounts_prior_period", pval,
                      [self._ev(r, "official_accounts", pspan, "brreg_regnskap_api", reporting_period=pperiod)],
                      reporting_period=pperiod, effective_date=pp.get("tilDato"))
        hist = [f"{(x.get('regnskapsperiode') or {}).get('fraDato')}..{(x.get('regnskapsperiode') or {}).get('tilDato')}" for x in records]
        self._add("accounts", "accounts_history", hist, [self._ev(r, "official_accounts", _span([x.get("regnskapsperiode") for x in records]), "brreg_regnskap_api")],
                  note="periods returned by the normalised accounts API; older filings exist as PDF copies at "
                       f"{BRREG}/regnskapsregisteret/regnskap/aarsregnskap/kopi/{self.org}/<year> (rate-limited, not fetched)")
        self.sections["accounts"] = AVAILABLE

    # ---- filing years ----------------------------------------------------------------------------------------
    def filing_years(self) -> None:
        """Every year for which an annual-account copy is on file. The normalised accounts endpoint returns only
        the latest period, so this is the only official view of the filing history without fetching PDFs."""
        if self.sections.get("accounts") == NOT_APPLICABLE:
            return
        _history_slot()
        r = self.s.get(f"{BRREG}/regnskapsregisteret/regnskap/aarsregnskap/kopi/{self.org}/aar", company=self.org, kind="json", robots=False)
        data = r.json() if r.ok else None
        if not r.ok or not isinstance(data, list):
            if r.status == 404:
                self._add("accounts", "accounts_filing_years", None, [self._ev(r, "official_accounts", f"HTTP {r.status}", "brreg_aarsregnskap_kopi_years")],
                          availability=NOT_AVAILABLE, note="no annual-account copies on file for this organisation")
            else:
                st = state_from_fetch(r)
                self.errors.append(new_error("filing_years", r.error or f"http_{r.status}", st, r.url))
                self._add("accounts", "accounts_filing_years", None, [self._ev(r, "official_accounts", f"HTTP {r.status}", "brreg_aarsregnskap_kopi_years")] if r.status else [],
                          availability=st, note=f"filing-years endpoint failed: {r.error or r.status}")
            return
        years = sorted({str(y) for y in data if str(y).strip().isdigit()})
        ev = self._ev(r, "official_accounts", _span(years), "brreg_aarsregnskap_kopi_years")
        self._add("accounts", "accounts_filing_years", years, [ev],
                  note=f"years with an annual-account copy on file; copies at {BRREG}/regnskapsregisteret/regnskap/aarsregnskap/kopi/{self.org}/<year> (not fetched)")
        if years:
            self._add("accounts", "first_filing_year", years[0], [ev], effective_date=f"{years[0]}-12-31")
            self._add("accounts", "filings_on_file", len(years), [ev])

    # ---- roles --------------------------------------------------------------------------------------------
    def roles(self) -> None:
        r = self.s.get(f"{BRREG}/enhetsregisteret/api/enheter/{self.org}/roller", company=self.org, kind="json", robots=False)
        data = r.json() if r.ok else None
        if not r.ok or not isinstance(data, dict):
            st = state_from_fetch(r)
            self.errors.append(new_error("roles", r.error or f"http_{r.status}", st, r.url))
            self._add("leadership", "role", None, [self._ev(r, "official_roles", f"HTTP {r.status}", "brreg_roller_api")] if r.status else [],
                      availability=st if st != NOT_AVAILABLE else NOT_AVAILABLE, note="no public roles returned by the registry" if r.status == 404 else f"roles endpoint failed: {r.error or r.status}")
            self.sections["leadership"] = st
            return
        n = 0
        for group in data.get("rollegrupper") or []:
            since = group.get("sistEndret")
            for role in group.get("roller") or []:
                if role.get("fratraadt"):
                    continue
                t = role.get("type") or {}
                person = role.get("person")
                enhet = role.get("enhet")
                if person:
                    nm = person.get("navn") or {}
                    name = " ".join(p for p in (nm.get("fornavn"), nm.get("mellomnavn"), nm.get("etternavn")) if p)
                    if person.get("erDod"):
                        continue
                    holder_org = None
                else:
                    name = " ".join(enhet.get("navn") or []) if enhet else None
                    holder_org = (enhet or {}).get("organisasjonsnummer")
                if not name:
                    continue
                value = {"name": name, "role": t.get("beskrivelse"), "role_code": t.get("kode"), "since": since,
                         "organisation_number": holder_org, "holder_type": "entity" if enhet else "person"}
                span = f"{t.get('beskrivelse')} ({t.get('kode')}): {name}; sistEndret {since}"
                self._add("leadership", "role", value, [self._ev(r, "official_roles", span, "brreg_roller_api")], effective_date=since)
                n += 1
        if n == 0:
            self._add("leadership", "role", None, [self._ev(r, "official_roles", _span(data), "brreg_roller_api")], availability=NOT_AVAILABLE,
                      note="registry returned no active public role holders")
            self.sections["leadership"] = NOT_AVAILABLE
        else:
            self._add("leadership", "role_count", n, [self._ev(r, "official_roles", f"{n} active roles", "brreg_roller_api")])
            self.sections["leadership"] = AVAILABLE

    # ---- subunits -----------------------------------------------------------------------------------------
    def workplaces(self) -> None:
        r = self.s.get(f"{BRREG}/enhetsregisteret/api/underenheter?overordnetEnhet={self.org}&size=200", company=self.org, kind="json", robots=False)
        data = r.json() if r.ok else None
        if not r.ok or not isinstance(data, dict):
            st = state_from_fetch(r)
            self.errors.append(new_error("subunits", r.error or f"http_{r.status}", st, r.url))
            self._add("workplaces", "workplace", None, [], availability=st, note=f"subunit endpoint failed: {r.error or r.status}")
            self.sections["workplaces"] = st
            return
        units = (data.get("_embedded") or {}).get("underenheter") or []
        units = [u for u in units if not u.get("slettedato")]
        for u in units:
            a = u.get("beliggenhetsadresse") or u.get("postadresse") or {}
            value = {"organisation_number": u.get("organisasjonsnummer"), "name": u.get("navn"), "address": _addr(a),
                     "employees": u.get("antallAnsatte") if u.get("harRegistrertAntallAnsatte", True) else None,
                     "industry": (u.get("naeringskode1") or {}).get("beskrivelse"), "started": u.get("oppstartsdato")}
            span = _span({"organisasjonsnummer": u.get("organisasjonsnummer"), "navn": u.get("navn"), "beliggenhetsadresse": a, "antallAnsatte": u.get("antallAnsatte")})
            self._add("workplaces", "workplace", value, [self._ev(r, "official_subunits", span, "brreg_underenheter_api")], effective_date=u.get("oppstartsdato"))
        total = (data.get("page") or {}).get("totalElements", len(units))
        self._add("workplaces", "workplace_count", len(units), [self._ev(r, "official_subunits", _span({"page": data.get("page")}), "brreg_underenheter_api")],
                  note=None if len(units) else "checked the registry; this entity has no registered subunits (zero, not missing)")
        if total and total > len(units):
            self.errors.append(new_error("subunits", f"{total} subunits registered, first {len(units)} fetched", AVAILABLE, r.url))
        self.sections["workplaces"] = AVAILABLE

    # ---- helpers ----------------------------------------------------------------------------------------
    def _ev(self, fetch, source_class: str, span: str, method: str, reporting_period: Optional[str] = None) -> str:
        ev = new_evidence(self.ids, fetch, source_class, span, method, reporting_period=reporting_period)
        self.evidence.append(ev)
        return ev["id"]

    def _add(self, section: str, field: str, value, evidence_ids: list[str], availability: str = AVAILABLE,
             confidence: float = 1.0, note: Optional[str] = None, reporting_period: Optional[str] = None,
             effective_date: Optional[str] = None) -> None:
        if availability == AVAILABLE and value is None:
            availability, note = NOT_AVAILABLE, note or "field absent in the official record"
        self.claims.append(new_claim(self.ids, section, field, value, availability, evidence_ids, confidence, note, reporting_period, effective_date))
