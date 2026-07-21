from datetime import datetime, timedelta, timezone
from fetch import is_expired, _detect_two_stage, _notice_url, _is_relevant_sector
from build_leads import classify_sector, dedupe_cross_source, classify_hvac_by_description
from backfill_deadlines import DEADLINE_PATTERNS, AWARD_SIGNAL, SUPPLIER_NAMED_SIGNAL, _parse_date


def test_is_expired():
    now = datetime.now(timezone.utc)

    # Future dates should not be expired
    future_dt = now + timedelta(days=5)
    assert not is_expired(future_dt.isoformat())
    assert not is_expired("2099-01-01")
    assert not is_expired("2099-01-01T12:00:00Z")

    # Past dates should be expired
    past_dt = now - timedelta(days=5)
    assert is_expired(past_dt.isoformat())
    assert is_expired("2025-08-01")
    assert is_expired("2025-08-01T23:59:59Z")
    assert is_expired("2025-08-01T23:59:59+01:00")

    # Empty/invalid hints should not be expired
    assert not is_expired("")
    assert not is_expired("invalid-date")


def test_detect_two_stage():
    tender_open = {
        "procurementMethodDetails": "Open procedure",
        "title": "Daily cleaning",
        "description": "Standard cleaning services",
    }
    assert not _detect_two_stage(tender_open)

    tender_two_stage = {
        "procurementMethodDetails": "Open competitive flex two-stage tender under the Procurement Act 2023",
        "title": "Daily cleaning",
        "description": "Standard cleaning services",
    }
    assert _detect_two_stage(tender_two_stage)

    tender_by_desc = {
        "procurementMethodDetails": "Other",
        "title": "Cleaning",
        "description": "This is a two-stage process. First stage requires Conditions of Participation.",
    }
    assert _detect_two_stage(tender_by_desc)


def test_notice_url():
    # CF notice carries its own URL
    cf_release = {
        "tender": {
            "documents": [
                {
                    "url": "https://www.contractsfinder.service.gov.uk/Notice/123-abc",
                    "documentType": "tenderNotice",
                }
            ]
        }
    }
    assert (
        _notice_url(cf_release, "ocds-123")
        == "https://www.contractsfinder.service.gov.uk/Notice/123-abc"
    )

    # FTS notice with URL in documents
    fts_doc_release = {
        "tender": {
            "documents": [
                {
                    "url": "https://www.find-tender.service.gov.uk/Notice/012345-2026",
                    "documentType": "tenderNotice",
                }
            ]
        }
    }
    assert (
        _notice_url(fts_doc_release, "ocds-abc")
        == "https://www.find-tender.service.gov.uk/Notice/012345-2026"
    )

    # FTS notice fallback to release ID
    fts_fallback_release = {"id": "012345-2026", "ocid": "ocds-h6vhtk-012345", "tender": {}}
    assert (
        _notice_url(fts_fallback_release, "ocds-h6vhtk-012345")
        == "https://www.find-tender.service.gov.uk/Notice/012345-2026"
    )

    # FTS fallback to ocid if release ID has no hyphen
    fts_no_hyphen_release = {"id": "not_a_slug", "ocid": "ocds-h6vhtk-012345", "tender": {}}
    assert (
        _notice_url(fts_no_hyphen_release, "ocds-h6vhtk-012345")
        == "https://www.find-tender.service.gov.uk/Notice/ocds-h6vhtk-012345"
    )


def test_classify_sector():
    # Title matches -> tagged
    assert classify_sector("Office Cleaning Services - Camden") == "cleaning"
    assert classify_sector("Manned Guarding and Keyholding Contract") == "security"
    assert classify_sector("School Catering Framework 2026") == "catering"

    # Regression: 2026-07-08 incident -- keyword buried in description body
    # (not title) used to mistag unrelated notices. Title-only match must
    # NOT fire on these even though the old description-scan would have.
    assert classify_sector("Enterprise Backup/Recovery & Cyber Resiliency Platform") is None
    assert classify_sector("The West Midlands Franchising Scheme for Buses") is None
    assert classify_sector("Medicines Licensing Assessor Services") is None

    # No match anywhere -> None, not a silent 'facilities' catch-all
    assert classify_sector("Supply of Traffic Signal Equipment") is None

    # Regression: word-boundary bugs found while fixing the description-scan
    # issue. Bare substrings inside unrelated words, and overly broad single
    # keywords, both produced false positives.
    assert classify_sector("Provision of Safeguarding Training for Group B and Group C") is None
    assert classify_sector("Water Management Control and Monitoring of Legionella") is None
    assert (
        classify_sector(
            "Procurement of traffic signal equipment, telecommunications "
            "equipment, and video surveillance systems"
        )
        is None
    )
    assert (
        classify_sector("CA18037 - Barlow RC High School - Building Cleaning Services")
        == "cleaning"
    )

    # specialist-client niche (their words). hvac must win over
    # cleaning when the title is duct/extract work, and fire it on the
    # HVAC-maintenance phrasing too.
    assert classify_sector("Kitchen Extract Ductwork Cleaning to TR19 Standard") == "hvac"
    assert classify_sector("Fire Damper Testing and Maintenance") == "hvac"
    assert classify_sector("HVAC and Ventilation System Maintenance") == "hvac"
    assert classify_sector("Duct Cleaning Services for School Kitchens") == "hvac"
    # Regression: "product"/"conductor" must NOT trip the bare-duct trap.
    assert classify_sector("Provision of Allograft Products") is None
    assert classify_sector("Conductor Rail Replacement Programme") is None


def test_classify_hvac_by_description():
    # West Suffolk Council (062971-2026, verified 2026-07-15): generic title,
    # but scope names the specialist's exact work. Title-only classify misses it; the
    # description scan must catch it.
    assert classify_sector("Mechanical Services Maintenance Contract 2026 - 2031") is None
    west_suffolk_desc = (
        "Repairs and replacement of CHP units. Replacement of flues. "
        "Kitchen extract duct cleaning. New boiler and heating services. "
        "Ventilation system duct cleaning. Minor building works."
    )
    assert classify_hvac_by_description(west_suffolk_desc)

    # Bare "hvac"/"air handling" in a broad M&E notice must NOT trigger the
    # description scan -- those are the phrases that false-positive, which is
    # why they're excluded from HVAC_DESC_PHRASES (only title match uses them).
    generic_me = (
        "Estates consultancy covering HVAC, air handling, electrical and "
        "life safety systems across the portfolio."
    )
    assert not classify_hvac_by_description(generic_me)

    # Empty / missing description is safe.
    assert not classify_hvac_by_description("")
    assert not classify_hvac_by_description(None)


def test_is_relevant_sector_catering():
    # Catering-titled release with no CPV items -> caught by keyword fallback.
    catering_release = {
        "tender": {
            "title": "School Catering Services Contract",
            "description": "",
            "items": [],
        }
    }
    assert _is_relevant_sector(catering_release)

    # Regression: unrelated sector still not swept in by the new keywords.
    it_release = {
        "tender": {
            "title": "IT Support Services",
            "description": "",
            "items": [],
        }
    }
    assert not _is_relevant_sector(it_release)


def test_backfill_award_and_deadline_signals():
    # Ground truth captured 2026-07-12 from live FTS notice pages via manual
    # investigation after the first backfill run returned 0/36 recovered.

    # Attain Academy Partnership (061462... err 064417-2026) — genuinely
    # open two-stage tender. Has a real deadline, no named Suppliers.
    attain_text = (
        "Submission Submission Enquiry deadline 20 July 2026, 12:00pm "
        "Submission type Requests to participate Deadline for requests to "
        "participate 2 August 2026, 11:59pm Submission address and any "
        "special instructions Tenders and enquiries to be emailed to "
        "Tenders@attain.essex.sch.uk"
    )
    assert not AWARD_SIGNAL.search(attain_text)
    assert not SUPPLIER_NAMED_SIGNAL.search(attain_text)
    matched = None
    for p in DEADLINE_PATTERNS:
        m = p.search(attain_text)
        if m:
            matched = m.group(1)
            break
    assert matched == "2 August 2026, 11:59pm"
    assert _parse_date(matched) == "2026-08-02T23:59:00+00:00"

    # Barnes Primary School (061536-2026) — awarded. No "deadline" word, no
    # "Award decision date" phrase either -- FTS serves the award notice at
    # the same slug with a named Supplier right after "Submission type".
    barnes_text = (
        "Submission Submission Submission type Tenders Procedure Procedure "
        "Procedure type Competitive flexible procedure Supplier Supplier "
        "Ever brite Companies House: 01307015 Unit J Merlin Centre"
    )
    assert not AWARD_SIGNAL.search(barnes_text)
    assert SUPPLIER_NAMED_SIGNAL.search(barnes_text)
    assert not any(p.search(barnes_text) for p in DEADLINE_PATTERNS)

    # Harris Federation (061873-2026) — same pattern, plural "Suppliers".
    harris_text = (
        "Submission Submission Submission type Requests to participate "
        "Other information Other information Applicable trade agreements "
        "Government Procurement Agreement (GPA) Conflicts assessment "
        "prepared/revised Yes Procedure Procedure Procedure type "
        "Competitive flexible procedure Suppliers Suppliers Atlas "
        "Facilities Management Limited Companies House: 02633080"
    )
    assert SUPPLIER_NAMED_SIGNAL.search(harris_text)

    # The Chase School — awarded, concrete PAST date. Kill signal fires.
    chase_text = "Award decision date 30 June 2026 Date assessment summaries were sent to tenderers 30 June 2026"
    m = AWARD_SIGNAL.search(chase_text)
    assert m
    assert m.group(1) is None  # not "(estimated)"
    assert _parse_date(m.group(2)) == "2026-06-30T00:00:00+00:00"

    # Regression: "Award decision date (estimated) <FUTURE date>" is a LIVE
    # two-stage tender publishing its planned timeline, NOT an award. Firing
    # on the bare phrase marked Attain expired (1970) and fetch.py's save()
    # then dropped it from the raw store forever. The regex may match, but
    # the (estimated) flag + future date must prevent the kill in investigate().
    estimated_future = "Award decision date (estimated) 8 September 2026 Standstill period"
    m2 = AWARD_SIGNAL.search(estimated_future)
    assert m2
    assert m2.group(1) == "(estimated)"  # investigate() must NOT expire this


def test_dedupe_cross_source():
    # Same live tender published on BOTH central FTS and a ProContract
    # eSourcing portal. FTS copy has no usable deadline ('unknown'); the
    # ProContract copy carries the real Expression End date. Merge must keep
    # ONE row, carry the ProContract deadline, keep the FTS canonical URL.
    fts = {
        "id": "ocds-h6vhtk-06c196",
        "title": "Maintenance and Servicing of Grounds Maintenance Equipment",
        "buyer": "Vale of White Horse District Council",
        "deadline_hint": "unknown",
        "url": "https://www.find-tender.service.gov.uk/Notice/012-2026",
        "value_hint": "£120,000",
    }
    procontract = {
        "id": "procontract-DN819551",
        "title": "Maintenance and servicing of grounds maintenance equipment",
        "buyer": "Vale of White Horse District Council",
        "deadline_hint": "2026-08-04T12:00:00+00:00",
        "url": "https://procontract.due-north.com/Opportunities/xyz",
        "value_hint": "",
    }
    out = dedupe_cross_source([fts, procontract])
    assert len(out) == 1
    m = out[0]
    assert m["deadline_hint"] == "2026-08-04T12:00:00+00:00"  # from ProContract
    assert m["url"] == "https://www.find-tender.service.gov.uk/Notice/012-2026"  # FTS canonical
    assert m["value_hint"] == "£120,000"  # filled from FTS

    # Two genuinely different tenders from the same buyer are NOT merged.
    a = {
        "id": "ocds-1",
        "title": "Window Cleaning",
        "buyer": "Camden",
        "deadline_hint": "2026-09-01",
    }
    b = {
        "id": "ocds-2",
        "title": "Manned Guarding",
        "buyer": "Camden",
        "deadline_hint": "2026-09-02",
    }
    assert len(dedupe_cross_source([a, b])) == 2

    # A single record passes through untouched.
    assert len(dedupe_cross_source([a])) == 1


def test_digest_client_matching_and_render():
    from datetime import date
    import pytest
    from digest_client import LEADS_CSV, matching_tenders, render

    if not LEADS_CSV.exists():
        pytest.skip("integration test: run fetch.py + build_leads.py first")

    profile = {
        "name": "the specialist client Clean Ltd",
        "contact_first_name": "the client",
        "match_sectors": ["hvac", "cleaning"],
        "match_regions": ["suffolk", "essex"],
    }
    # Live DB integration: every match must be in profile sectors and regions.
    matches = matching_tenders(profile)
    for m in matches:
        assert m["sector"] in {"hvac", "cleaning"}

    # Render honesty: empty week says so plainly, no fabricated tenders.
    empty = render(profile, [], date(2026, 7, 18))
    assert "Quiet week" in empty and "http" not in empty

    # Render with one tender: deadline countdown + moat flag + url present.
    row = {
        "id": "procontract-DN000001",
        "title": "Kitchen Extract Cleaning",
        "buyer": "Test Council",
        "deadline": "2026-07-28",
        "value": "",
        "url": "https://procontract.due-north.com/x",
        "sector": "hvac",
        "location": "",
    }
    out = render(profile, [row], date(2026, 7, 18))
    assert "the client," in out
    assert "(10 days)" in out
    assert "eSourcing portal only" in out
    assert "https://procontract.due-north.com/x" in out


def test_normalize_award():
    from fetch_awards import _normalize_award

    pkg = {
        "ocid": "ocds-h6vhtk-abc123",
        "buyer": {"name": "Test Council"},
        "tender": {"title": "Cleaning Services"},
        "awards": [
            {
                "id": "aw-1",
                "date": "2026-06-15T00:00:00Z",
                "suppliers": [{"name": "Winner Ltd"}, {"name": "Partner Ltd"}],
                "value": {"amount": 250000, "currency": "GBP"},
                "contractPeriod": {
                    "startDate": "2026-09-01T00:00:00Z",
                    "endDate": "2029-08-31T00:00:00Z",
                },
            },
            # lot with no supplier named -> skipped (nothing to cite)
            {"id": "aw-2", "suppliers": []},
        ],
    }
    recs = _normalize_award(pkg)
    assert len(recs) == 1
    r = recs[0]
    assert r["winner"] == "Winner Ltd; Partner Ltd"
    assert r["award_date"] == "2026-06-15"
    assert r["contract_end"] == "2029-08-31"
    assert r["value"] == 250000
    assert r["id"] == "ocds-h6vhtk-abc123-aw-1"

    # release with no awards at all -> empty
    assert _normalize_award({"ocid": "x", "awards": []}) == []


def test_awards_csv_windows(monkeypatch):
    """recent-wins keeps only 90-day awards; expiring-soon only contracts
    ending within 12 months and not already ended. (Uses tempfile instead of
    the tmp_path fixture -- pytest's temp root is permission-broken on this
    machine's Windows Store Python.)"""
    import tempfile
    from datetime import datetime, timezone
    from pathlib import Path as _Path
    import fetch_awards

    tmp_path = _Path(tempfile.mkdtemp())
    monkeypatch.setattr(fetch_awards, "OUT_WINS", tmp_path / "wins.csv")
    monkeypatch.setattr(fetch_awards, "OUT_EXPIRING", tmp_path / "expiring.csv")
    today = datetime(2026, 7, 19, tzinfo=timezone.utc)

    def rec(id_, title, award_date, contract_end):
        return {
            "id": id_,
            "ocid": id_,
            "buyer": "B",
            "title": title,
            "winner": "W",
            "value": 1,
            "currency": "GBP",
            "award_date": award_date,
            "contract_start": "",
            "contract_end": contract_end,
        }

    store = [
        rec("fresh", "Office Cleaning Contract", "2026-06-01", "2029-01-01"),  # wins: yes
        rec(
            "stale", "Office Cleaning Contract", "2025-01-01", "2026-12-01"
        ),  # wins: no (old); expiring: yes
        rec(
            "ended", "Office Cleaning Contract", "2025-01-01", "2026-01-01"
        ),  # both: no (already ended)
        rec("far", "Office Cleaning Contract", "2026-06-01", "2031-01-01"),  # expiring: no (>12mo)
        rec(
            "offsector", "Road Resurfacing Works", "2026-06-01", "2026-12-01"
        ),  # both: no (not our sector)
    ]
    fetch_awards.build_csvs(store, today)

    import csv as _csv

    wins = {r["ocid"] for r in _csv.DictReader((tmp_path / "wins.csv").open(encoding="utf-8"))}
    expiring = {
        r["ocid"] for r in _csv.DictReader((tmp_path / "expiring.csv").open(encoding="utf-8"))
    }
    assert wins == {"fresh", "far"}
    assert expiring == {"stale"}
