"""Pull live UK tenders from Find a Tender + Contracts Finder (free OCDS APIs, no auth).

Two official sources, both paged as OCDS release packages:
  FTS (above-threshold, ~GBP139k+): https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages
  Contracts Finder (below-threshold England): https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search

We page through recent releases, keep cleaning-related notices (CPV 90900000 family
or keyword match), normalize each to the flat record the classifier runner expects,
and append to data/tenders-raw.jsonl (dedup by ocid across both sources).

Usage:
    uv run python fetch.py                 # last 7 days, cleaning only
    uv run python fetch.py --days 30       # widen window
    uv run python fetch.py --all-sectors   # skip the cleaning filter (debug)

If the API is unreachable from your network, the pipeline still runs on
data/tenders-fixture.jsonl — fetch is the only piece that needs the internet.
"""

import argparse
import json
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

FTS_API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CF_API = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
PCS_API = "https://api.publiccontractsscotland.gov.uk/v1/Notices"
S2W_API = "https://api-sell2wales.klickstream.com/v1"
RAW_PATH = Path("data/tenders-raw.jsonl")

# CPV prefixes for our target sectors.
# 909 = cleaning & sanitation, 797 = security, 983 = facilities management,
# 507 = building maintenance, 905 = pest control, 773 = grounds/landscaping,
# 553 = catering services (division 55 "hotel, restaurant and retail trade
# services" also covers 551/552/554/555 hotels, restaurants, bars, etc. --
# deliberately narrowed to 553 so we don't pull in hospitality-as-buyer
# notices that have nothing to do with our catering-contractor leads).
RELEVANT_CPV_PREFIXES = ("909", "797", "983", "507", "905", "773", "553")

# Keyword fallback for notices that don't tag CPV cleanly.
CLEANING_KEYWORDS = (
    "cleaning",
    "janitorial",
    "caretaking",
    "custodial",
    "housekeeping",
    "window cleaning",
    "deep clean",
    "hygiene",
    "sanitisation",
    "sanitization",
)
SECURITY_KEYWORDS = (
    "security",
    "guarding",
    "patrol",
    "keyholding",
    "key holding",
    "surveillance",
    "concierge",
    "cctv",
    "access control",
    "manned guarding",
    "door supervision",
    "door supervisor",
    "close protection",
    "static guard",
    "mobile patrol",
    "alarm response",
    "event security",
    "stewarding",
)
FM_KEYWORDS = (
    "facilities management",
    "soft services",
    "hard services",
    "building maintenance",
    "grounds maintenance",
    "landscaping",
    "pest control",
    "waste management",
    "porterage",
    "portering",
    "caretaker",
    "reception services",
    "helpdesk",
)
# No bare "food" (catches food-safety/hygiene-inspection tenders unrelated
# to catering services) and no bare "kitchen" (catches kitchen EQUIPMENT
# supply/servicing tenders -- see the Embark MAT dynamic-market notice in
# data/tenders-raw.jsonl, which lists "Catering / kitchen equipment
# servicing/maintenance" as a category with nothing to do with catering
# service provision). Kept as a superset of build_leads.py's
# classify_sector() CATERING_KEYWORDS (catering/canteen/kitchen) but
# qualified where this module's broader title+description scan would
# otherwise pick up false positives that a title-only match wouldn't.
CATERING_KEYWORDS = (
    "catering",
    "canteen",
    "kitchen services",
    "meal provision",
    "school meals",
    "food service",
)
# A specialist client's actual niche, defined verbatim in their reply:
# "kitchen duct, fire damper and anything duct, may come under HVAC
# maintenance". This is a SPECIALIST national market -- rare, and buyers
# are anywhere -- so a 5-day regional pull almost never catches it. These
# keywords ride the daily fetch AND a wider periodic sweep (see
# --days on the CLI / the weekly national sweep job). Phrases chosen to
# avoid the false positives the CATERING notes above warn about: bare
# "kitchen"/"duct" catch equipment-supply and "product"; require the
# extract/ventilation/damper qualifier.
HVAC_DUCT_KEYWORDS = (
    "kitchen extract",
    "extract cleaning",
    "extract ventilation",
    "ductwork",
    "duct cleaning",
    "fire damper",
    "ventilation cleaning",
    "ventilation maintenance",
    "ventilation system",
    "hvac",
    "air handling",
    "grease extract",
    "local exhaust ventilation",
    "lev testing",
    "tr19",
    "kitchen exhaust",
)
ALL_KEYWORDS = (
    CLEANING_KEYWORDS + SECURITY_KEYWORDS + FM_KEYWORDS + CATERING_KEYWORDS + HVAC_DUCT_KEYWORDS
)


def is_expired(deadline_hint: str) -> bool:
    if not deadline_hint:
        return False
    try:
        # datetime.fromisoformat handles YYYY-MM-DD and full ISO strings
        dt = datetime.fromisoformat(deadline_hint.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except ValueError:
        return False


def _detect_two_stage(tender: dict) -> bool:
    method = str(tender.get("procurementMethodDetails", "")).lower()
    title = str(tender.get("title", "")).lower()
    desc = str(tender.get("description", "")).lower()
    keywords = (
        "two-stage",
        "two stage",
        "competitive flex",
        "conditions of participation",
        "flexible procedure",
    )
    return (
        "two-stage" in method
        or "flexible" in method
        or any(k in title or k in desc for k in keywords)
    )


def _is_relevant_sector(release: dict) -> bool:
    tender = release.get("tender", {})
    for item in tender.get("items", []):
        cpv = (item.get("classification", {}) or {}).get("id", "")
        if any(str(cpv).startswith(p) for p in RELEVANT_CPV_PREFIXES):
            return True
    blob = f"{tender.get('title', '')} {tender.get('description', '')}".lower()
    return any(k in blob for k in ALL_KEYWORDS)


def _notice_url(release: dict, ocid: str) -> str:
    """Contracts Finder and FTS carry their own notice URLs in tender.documents or associated records.
    If none found for FTS, fallback to release ID notice slug format."""
    # 1. Search tender documents
    for doc in release.get("tender", {}).get("documents", []) or []:
        url = doc.get("url", "") or ""
        if (
            "contractsfinder.service.gov.uk/Notice/" in url
            or "find-tender.service.gov.uk/Notice/" in url
        ):
            return url

    # 2. Search awards documents
    for award in release.get("awards", []) or []:
        for doc in award.get("documents", []) or []:
            url = doc.get("url", "") or ""
            if "find-tender.service.gov.uk/Notice/" in url:
                return url

    # 3. Search contracts documents
    for contract in release.get("contracts", []) or []:
        for doc in contract.get("documents", []) or []:
            url = doc.get("url", "") or ""
            if "find-tender.service.gov.uk/Notice/" in url:
                return url

    # 4. Fallback for FTS: if release ID is formatted as numeric-year (e.g. 063652-2026), use it
    rid = release.get("id") or ""
    if rid and "-" in rid:
        return f"https://www.find-tender.service.gov.uk/Notice/{rid}"

    return f"https://www.find-tender.service.gov.uk/Notice/{ocid}"


def _normalize(release: dict) -> dict | None:
    """OCDS release -> flat record the classifier consumes. None if unusable."""
    tender = release.get("tender", {})
    ocid = release.get("ocid") or release.get("id")
    title = tender.get("title")
    if not ocid or not title:
        return None

    buyer = (release.get("buyer", {}) or {}).get("name", "Unknown buyer")
    period = tender.get("tenderPeriod", {}) or {}
    deadline = period.get("endDate")
    if not deadline:
        if _detect_two_stage(tender):
            deadline = "unknown - two-stage check needed"
        else:
            deadline = "unknown"

    addr = ""
    items = tender.get("items", [])
    if items:
        delivery = items[0].get("deliveryAddresses") or items[0].get("deliveryAddress")
        if isinstance(delivery, list) and delivery:
            addr = delivery[0].get("region") or delivery[0].get("locality") or ""
        elif isinstance(delivery, dict):
            addr = delivery.get("region") or delivery.get("locality") or ""

    value = tender.get("value", {}) or {}
    value_str = (
        f"{value.get('amount')} {value.get('currency')}".strip() if value.get("amount") else ""
    )

    return {
        "id": str(ocid),
        "title": title,
        "buyer": buyer,
        "url": _notice_url(release, str(ocid)),
        "location": addr,
        "deadline_hint": deadline,
        "value_hint": value_str,
        "description": tender.get("description", "") or "",
    }


def _get_page(client: httpx.Client, url: str, params: dict | None) -> dict | None:
    """Fetch one OCDS page, resilient to FTS quirks:
      - 429 Too Many Requests -> back off (honor Retry-After) and retry
      - 200 with truncated/malformed JSON -> retry once, then give up on the page
    Returns the parsed page, or None if unrecoverable (caller keeps prior pages)."""
    for attempt in range(4):
        resp = client.get(url, params=params)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 5)) + attempt * 2
            print(f"  429 rate-limited — waiting {wait:.0f}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            if attempt < 3:
                print(f"  page parse failed: {e} — retrying")
                time.sleep(2.0)
                continue
            print(f"  page parse failed repeatedly: {e} — stopping pagination here")
            return None
    print("  gave up after repeated 429s")
    return None


def _fetch_source(
    client: httpx.Client,
    name: str,
    first_url: str,
    params: dict,
    all_sectors: bool,
    tender_tags_only: bool,
    max_pages: int,
) -> list[dict]:
    """Page one OCDS source (both APIs use releases[] + links.next cursors)."""
    out: list[dict] = []
    url: str | None = first_url
    page_params: dict | None = params
    for page in range(max_pages):
        data = _get_page(client, url, page_params)
        if data is None:  # unrecoverable bad page — keep what we have, stop
            print(f"  {name}: stopped at page {page + 1} on a bad response; kept {len(out)}")
            break
        for pkg in data.get("releases", []):
            # For both FTS and Contracts Finder, we only want tender/tenderUpdate releases.
            # Avoid award and contract releases since those represent completed/closed procurements.
            tags = pkg.get("tag") or []
            is_tender = any(t in ("tender", "tenderUpdate") for t in tags)
            if tender_tags_only and not is_tender:
                continue

            # Check if tender status is complete, cancelled, or unsuccessful
            tender_status = (pkg.get("tender", {}) or {}).get("status")
            if tender_status in ("complete", "unsuccessful", "cancelled", "withdrawn"):
                continue

            if all_sectors or _is_relevant_sector(pkg):
                rec = _normalize(pkg)
                if rec:
                    out.append(rec)
        url = (data.get("links", {}) or {}).get("next")
        page_params = None  # next link already carries the query string
        if not url:
            break
        time.sleep(1.5)  # be polite — both APIs rate-limit
    print(f"  {name}: {len(out)} matched")
    return out


def _fetch_pcs(days: int, all_sectors: bool) -> list[dict]:
    """Public Contracts Scotland OCDS API (verified live 2026-07-10, see
    TENDER-SOURCES.md). One request per run — PCS rate-limits aggressively
    (we got a 403 cooldown within ~5 rapid requests while probing).

    noticeType=2 selects contract notices server-side, so no award-tag
    filtering is needed (and PCS releases don't reliably carry OCDS tags).
    dateFrom is DD-MM-YYYY.

    Cert note: PCS's server does not send its Sectigo intermediate cert,
    so default verification fails on EVERY host (confirmed on both local
    Windows and the Ubuntu CI runner). Fix: we carry the intermediate in
    certs/sectigo-dv-r36.pem (valid to 2036) and add it to the default
    trust store at runtime. Verification stays fully ON.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%m-%Y")
    params = {"noticeType": "2", "outputType": "0", "dateFrom": since}
    out: list[dict] = []
    intermediate = Path(__file__).parent / "certs" / "sectigo-dv-r36.pem"
    try:
        ctx = ssl.create_default_context()
        if intermediate.exists():
            ctx.load_verify_locations(cafile=str(intermediate))
        with httpx.Client(
            timeout=40.0, headers={"Accept": "application/json"}, verify=ctx
        ) as pcs_client:
            resp = pcs_client.get(PCS_API, params=params)
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError as e:
        if "CERTIFICATE_VERIFY" in str(e):
            print(f"  PCS: cert verify failed even with bundled intermediate — skipped ({e})")
        else:
            print(f"  PCS: connection failed — skipped ({e})")
        return out
    except (httpx.HTTPStatusError, json.JSONDecodeError) as e:
        print(f"  PCS: fetch failed — skipped ({e})")
        return out

    for pkg in data.get("releases", []):
        tender_status = (pkg.get("tender", {}) or {}).get("status")
        if tender_status in ("complete", "unsuccessful", "cancelled", "withdrawn"):
            continue
        if all_sectors or _is_relevant_sector(pkg):
            rec = _normalize(pkg)
            if rec:
                out.append(rec)
    print(f"  PCS: {len(out)} matched")
    return out


def _fetch_s2w(days: int, all_sectors: bool) -> list[dict]:
    """Sell2Wales OCDS API (documented at sell2wales.gov.wales/helpandresources/
    ocds/dataaccessinfo, see TENDER-SOURCES.md). Same Millstream/Proactis
    platform family as PCS — mirrors _fetch_pcs's request shape and error
    handling. Both this and PCS return an identical 403 signature from every
    IP tested so far (local India, GitHub Actions runner) despite no cert
    error — TLS succeeds, the block is at the HTTP/WAF layer. Best guess:
    both sites geo-block non-UK traffic outright, which no cert fix or
    pacing change can work around. Wired anyway on the chance the GitHub
    runner's IP range is treated differently than PCS's was; if it also
    403s consistently, treat both as dead ends per TENDER-SOURCES.md.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%m-%Y")
    params = {"lang": "en", "noticeType": "2", "outputType": "0", "dateFrom": since}
    out: list[dict] = []
    try:
        with httpx.Client(timeout=40.0, headers={"Accept": "application/json"}) as s2w_client:
            resp = s2w_client.get(S2W_API, params=params)
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError as e:
        print(f"  Sell2Wales: connection failed — skipped ({e})")
        return out
    except (httpx.HTTPStatusError, json.JSONDecodeError) as e:
        print(f"  Sell2Wales: fetch failed — skipped ({e})")
        return out

    for pkg in data.get("releases", []):
        tender_status = (pkg.get("tender", {}) or {}).get("status")
        if tender_status in ("complete", "unsuccessful", "cancelled", "withdrawn"):
            continue
        if all_sectors or _is_relevant_sector(pkg):
            rec = _normalize(pkg)
            if rec:
                out.append(rec)
    print(f"  Sell2Wales: {len(out)} matched")
    return out


def fetch(days: int, all_sectors: bool, max_pages: int = 50) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    with httpx.Client(
        timeout=30.0, headers={"Accept": "application/json"}, follow_redirects=True
    ) as client:
        # FTS contains awards; we must set tender_tags_only=True to skip them
        fts = _fetch_source(
            client,
            "FTS",
            FTS_API,
            {"updatedFrom": since, "limit": 100},
            all_sectors,
            tender_tags_only=True,
            max_pages=max_pages,
        )
        # ONE fetch, not a keyword loop. Verified 2026-07-10: the CF OCDS
        # search endpoint IGNORES the q param entirely — a nonsense word
        # ('zzqxblorp'), 'cleaning', and 'guarding' all returned byte-identical
        # release pages. The old 22-keyword loop fetched the same generic
        # recent-notices feed 22 times, eating ~40 min of 429 backoffs for
        # zero extra coverage. Sector filtering happens client-side in
        # _is_relevant_sector(), same as before.
        cf_records = _fetch_source(
            client,
            "ContractsFinder",
            CF_API,
            {"publishedFrom": since, "size": 100},
            all_sectors,
            tender_tags_only=True,
            max_pages=max_pages,
        )
        pcs_records = _fetch_pcs(days, all_sectors)
        s2w_records = _fetch_s2w(days, all_sectors)
    return fts + cf_records + pcs_records + s2w_records  # save() dedupes by ocid


def save(records: list[dict]) -> int:
    RAW_PATH.parent.mkdir(exist_ok=True)
    existing = {}
    if RAW_PATH.exists():
        for line in RAW_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            # Retroactive audit: skip expired tenders already in database
            if is_expired(r.get("deadline_hint", "")):
                continue
            existing[r["id"]] = r
    before = len(existing)

    # Merge new records, filtering out any that are expired
    for r in records:
        if is_expired(r.get("deadline_hint", "")):
            continue
        existing[r["id"]] = r

    RAW_PATH.write_text("\n".join(json.dumps(v) for v in existing.values()), encoding="utf-8")
    return len(existing) - before


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--all-sectors", action="store_true")
    args = parser.parse_args()

    print(f"Fetching FTS notices updated in last {args.days} days...")
    records = fetch(args.days, args.all_sectors)
    added = save(records)
    print(f"  matched {len(records)} notices, {added} new -> {RAW_PATH}")
    if records:
        print(f"  sample: {records[0]['title']}  ({records[0]['buyer']})")


if __name__ == "__main__":
    main()
