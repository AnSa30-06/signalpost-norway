"""Offline tests for the NAV feed connector: scan, name candidates, org-number confirmation, honest counts."""
from __future__ import annotations

import json

from signalpost import navfeed
from signalpost.net import FetchResult

BASE = navfeed.BASE


def page(items, next_url=None):
    return json.dumps({"items": items, "next_url": next_url, "next_id": None, "id": "p"})


def item(uuid, name, title, status="ACTIVE", when="2026-09-01T10:00:00+02:00"):
    return {"id": uuid, "url": f"/api/v1/feedentry/{uuid}", "title": title, "date_modified": when,
            "_feed_entry": {"uuid": uuid, "status": status, "title": title, "businessName": name, "municipal": "OSLO", "sistEndret": when}}


def entry(uuid, name, orgnr, title="Elektriker"):
    return json.dumps({"uuid": uuid, "status": "ACTIVE", "sistEndret": "2026-09-01T10:00:00+02:00",
                       "ad_content": {"title": title, "published": "2026-09-01T00:00:00+02:00", "expires": "2026-09-30T23:59:59+02:00",
                                      "link": f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}",
                                      "employer": {"name": name, "orgnr": orgnr}, "workLocations": [{"city": "OSLO", "municipal": "OSLO"}],
                                      "contactList": [{"name": "Kari", "phone": "12345678"}]}})


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, company, kind="html", headers=None, robots=True, max_bytes=0):
        self.calls.append(url)
        status, body = self.responses.get(url, (404, ""))
        r = FetchResult(url=url, final_url=url, status=status, body=body.encode(), text=body, retrieved_at="2026-09-09T10:00:00Z",
                        sha256="cd" * 32, snapshot_path="snapshots/x.json", content_type="application/json")
        if status >= 400:
            r.error = f"http_{status}"
        return r

    def remaining(self, company):
        return 99


def build_index(extra=None):
    responses = {
        navfeed.TOKEN_URL: (200, "Current public token:\neyJabc.def.ghi"),
        navfeed.FEED_URL: (200, page([item("u1", "AF Gruppen", "Elektriker"), item("u2", "AF Gruppen Norge AS", "HMS-rådgiver"),
                                      item("u3", "Sandnes Elektriske AS", "Montør"), item("u4", "Gammel AS", "Old", status="INACTIVE")])),
        BASE + "/api/v1/feedentry/u1": (200, entry("u1", "AF GRUPPEN ASA", "938702675")),
        BASE + "/api/v1/feedentry/u2": (200, entry("u2", "AF GRUPPEN NORGE AS", "999999999", "HMS-rådgiver")),
        BASE + "/api/v1/feedentry/u3": (200, entry("u3", "SANDNES ELEKTRISKE AS", "810034882", "Montør")),
    }
    responses.update(extra or {})
    s = FakeSession(responses)
    ix = navfeed.FeedIndex(s, days=60, chains=1)
    assert ix.build()
    return s, ix


def test_scan_indexes_active_ads_only():
    s, ix = build_index()
    assert ix.pages == 1 and ix.summary()["active_ads"] == 3
    assert "gammel as" not in ix.by_name


def test_exact_orgnr_confirmation_rejects_sister_company():
    s, ix = build_index()
    res = navfeed.fetch({"organisation_number": "938702675", "name": "AF GRUPPEN ASA"}, s, ix)
    jobs = [c for c in res["claims"] if c["field"] == "job_posting"]
    assert len(jobs) == 1 and jobs[0]["value"]["title"] == "Elektriker" and jobs[0]["value"]["employer_orgnr"] == "938702675"
    assert jobs[0]["value"]["source"] == "nav_feed" and jobs[0]["evidence_ids"]
    count = [c for c in res["claims"] if c["field"] == "active_job_count"][0]
    assert count["value"] == 1 and count["availability"] == "available"
    assert "contactList" not in json.dumps(res["claims"])  # contacts are never published


def test_zero_is_explicit_and_names_rejected_ads():
    s, ix = build_index()
    res = navfeed.fetch({"organisation_number": "111111111", "name": "AF GRUPPEN NORGE AS"}, s, ix)
    count = [c for c in res["claims"] if c["field"] == "active_job_count"][0]
    assert count["value"] == 0 and count["availability"] == "available" and "rejected on org number" in count["note"]


def test_unfetchable_record_makes_count_unknown_not_zero():
    s, ix = build_index({BASE + "/api/v1/feedentry/u3": (500, "")})
    res = navfeed.fetch({"organisation_number": "810034882", "name": "SANDNES ELEKTRISKE AS"}, s, ix)
    count = [c for c in res["claims"] if c["field"] == "active_job_count"][0]
    assert count["availability"] == "failed" and count["value"] is None


def test_unbuilt_index_is_failed():
    s = FakeSession({navfeed.TOKEN_URL: (500, "")})
    ix = navfeed.FeedIndex(s, days=60, chains=1)
    assert not ix.build()
    res = navfeed.fetch({"organisation_number": "810034882", "name": "SANDNES ELEKTRISKE AS"}, s, ix)
    assert res["claims"][0]["availability"] == "failed" and res["errors"]
