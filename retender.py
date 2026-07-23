"""Re-tender radar: mine AWARD notices for contracts expiring soon (P2).

Contracts Finder's REST search returns awarded contracts with a contract-end
date, the incumbent supplier and value. A contract ending in the next few months
means a re-tender is coming — usually published 3-6 months before the end date.
Nobody alerts small firms to these before the notice exists; this module does.

Two-stage region filter: CF's server-side `regions` filter is loose (national
frameworks list "Any region"), so we also require a profile region term to appear
in the notice's postcode/regionText/buyer/description. Precision over recall — a
short accurate list of nearby expiring contracts sells better than a noisy one.

Usage:
    uv run python retender.py                          # cleaning profile, dry scan
    uv run python retender.py --profile security --lookback-years 5 --horizon 270

State:
    data/expiries-<profile>.jsonl        all matched expiries, deduped by notice id
    data/seen-expiries-<profile>.json    ids already included in a past digest
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from fetch import CLEANING_KEYWORDS
from profile import FirmProfile, load_profile

DATA_DIR = Path("data")

# Contracts Finder REST search: unlike the OCDS feed this filters server-side by
# keyword, notice status AND region, and each hit carries the contract period
# (start/end), awarded supplier and value directly.
CF_REST_API = "https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json"

_CF_REGIONS = ("London", "South East")  # region values CF understands
_REGION_STOPWORDS = {"and", "the", "home", "counties", "england", "united", "kingdom", "greater"}
# London + South East postcode-area prefixes, to catch notices whose text names a
# town we don't list but whose postcode betrays the region.
_SE_POSTCODE_AREAS = (
    "e",
    "ec",
    "n",
    "nw",
    "se",
    "sw",
    "w",
    "wc",  # London
    "br",
    "cr",
    "da",
    "en",
    "ha",
    "ig",
    "kt",
    "rm",
    "sm",
    "tw",
    "ub",
    "wd",  # Greater London fringe
    "gu",
    "rh",
    "me",
    "ct",
    "tn",
    "bn",
    "so",
    "po",
    "rg",
    "sl",
    "ox",
    "hp",
    "al",
    "cm",
    "ss",  # SE counties
)


def _region_terms(firm: FirmProfile) -> set[str]:
    words = re.findall(r"[a-z]+", firm.region.lower())
    return {w for w in words if len(w) >= 4 and w not in _REGION_STOPWORDS}


def _cf_regions(firm: FirmProfile) -> list[str]:
    text = firm.region.lower()
    regions = [r for r in _CF_REGIONS if r.lower() in text]
    if "South East" not in regions and any(
        c in text for c in ("surrey", "kent", "essex", "herts", "home counties")
    ):
        regions.append("South East")
    return regions or list(_CF_REGIONS)


def _in_region(notice: dict, region_terms: set[str]) -> bool:
    """Second-pass region check: a profile region word in the notice text, or a
    London/SE postcode area. Guards against national frameworks CF tags 'Any region'."""
    text = " ".join(
        str(notice.get(k, "")) for k in ("regionText", "organisationName", "description", "title")
    ).lower()
    if any(term in text for term in region_terms):
        return True
    postcode = str(notice.get("postcode", "")).strip().lower()
    area = re.match(r"[a-z]{1,2}", postcode)
    return bool(area and area.group() in _SE_POSTCODE_AREAS)


def _to_record(
    notice: dict,
    keywords: tuple[str, ...],
    region_terms: set[str],
    horizon_end: datetime,
    now: datetime,
) -> dict | None:
    blob = f"{notice.get('title', '')} {notice.get('description', '')}".lower()
    if not any(k in blob for k in keywords):
        return None
    if not _in_region(notice, region_terms):
        return None
    end_raw = notice.get("end")
    nid = notice.get("noticeIdentifier") or notice.get("id")
    if not end_raw or not nid:
        return None
    try:
        end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if not (now < end <= horizon_end):
        return None

    suppliers = [
        html.unescape(s.strip())
        for s in (notice.get("awardedSupplier") or "").split(",")
        if s.strip()
    ]
    value = notice.get("awardedValue") or notice.get("valueHigh")
    # Drop national mega-frameworks: an SMB can't win a multi-hundred-supplier or
    # multi-million-pound framework, and bidding one wastes their time (the Stephen
    # rule, applied to expiries). Single-award contracts are the real opportunity.
    if len(suppliers) > 5:
        return None
    if isinstance(value, (int, float)) and value > 5_000_000:
        return None
    incumbent = suppliers[0] if suppliers else "not stated"
    if len(suppliers) > 1:
        incumbent += f" (+{len(suppliers) - 1} more on framework)"
    return {
        "id": str(nid),
        "title": html.unescape(notice.get("title", "")),
        "buyer": html.unescape(notice.get("organisationName", "Unknown buyer")),
        "incumbent": incumbent,
        "value_hint": f"{value:,.0f} GBP" if isinstance(value, (int, float)) and value else "",
        "contract_end": end.date().isoformat(),
        "url": f"https://www.contractsfinder.service.gov.uk/Notice/{nid}",
    }


def fetch_expiries(
    firm: FirmProfile,
    keywords: tuple[str, ...] = CLEANING_KEYWORDS,
    lookback_years: int = 5,
    horizon_days: int = 270,
    max_pages: int = 30,
) -> list[dict]:
    """Find in-region awarded contracts whose end date falls inside the horizon.

    Contracts expiring soon were AWARDED years ago (3-5yr terms are normal), so
    the search window reaches back lookback_years; the horizon filter on the
    contract `end` field does the real work client-side.
    """
    now = datetime.now(timezone.utc)
    horizon_end = now + timedelta(days=horizon_days)
    since = (now - timedelta(days=lookback_years * 365)).strftime("%Y-%m-%dT%H:%M:%S")
    region_terms = _region_terms(firm)

    found: dict[str, dict] = {}
    with httpx.Client(timeout=30.0, headers={"Accept": "application/json"}) as client:
        for page in range(1, max_pages + 1):
            body = {
                "searchCriteria": {
                    "keyword": keywords[0],
                    "statuses": ["Awarded"],
                    "publishedFrom": since,
                    "regions": _cf_regions(firm),
                },
                "size": 100,
                "page": page,
            }
            for attempt in range(4):
                resp = client.post(CF_REST_API, json=body)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 30)) + attempt * 10
                    print(f"  429 rate-limited — waiting {wait:.0f}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            else:
                print("  gave up after repeated 429s")
                return sorted(found.values(), key=lambda r: r["contract_end"])

            notices = [(hit.get("item") or hit) for hit in (resp.json().get("noticeList") or [])]
            for notice in notices:
                rec = _to_record(notice, keywords, region_terms, horizon_end, now)
                if rec:
                    found[rec["id"]] = rec
            if len(notices) < 100:
                break
            time.sleep(2.0)

    # Collapse near-duplicate notices for the same contract (same buyer + end date +
    # incumbent published under different notice ids), keeping the higher-value row.
    deduped: dict[tuple, dict] = {}
    for rec in found.values():
        key = (rec["buyer"].lower(), rec["contract_end"], rec["incumbent"].split(" (+")[0].lower())
        prior = deduped.get(key)

        def _val(r: dict) -> float:
            m = re.match(r"([\d,]+)", r["value_hint"])
            return float(m.group(1).replace(",", "")) if m else 0.0

        if prior is None or _val(rec) > _val(prior):
            deduped[key] = rec
    print(f"  ContractsFinder awards: {len(deduped)} contracts expiring within {horizon_days} days")
    return sorted(deduped.values(), key=lambda r: r["contract_end"])


def expiries_path(profile_name: str) -> Path:
    return DATA_DIR / f"expiries-{profile_name}.jsonl"


def seen_expiries_path(profile_name: str) -> Path:
    return DATA_DIR / f"seen-expiries-{profile_name}.json"


def save_expiries(profile_name: str, records: list[dict]) -> int:
    """Merge into the per-profile jsonl, dedupe by notice id. Returns count of new ids."""
    path = expiries_path(profile_name)
    path.parent.mkdir(exist_ok=True)
    existing: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                existing[rec["id"]] = rec
    before = len(existing)
    for r in records:
        existing[r["id"]] = r
    path.write_text("\n".join(json.dumps(v) for v in existing.values()), encoding="utf-8")
    return len(existing) - before


def load_new_expiries(profile_name: str) -> list[dict]:
    """Expiries not yet included in any digest for this profile."""
    path = expiries_path(profile_name)
    if not path.exists():
        return []
    seen_path = seen_expiries_path(profile_name)
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return sorted((r for r in records if r["id"] not in seen), key=lambda r: r["contract_end"])


def mark_expiries_seen(profile_name: str, records: list[dict]) -> None:
    seen_path = seen_expiries_path(profile_name)
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()
    seen |= {r["id"] for r in records}
    seen_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="cleaning")
    parser.add_argument("--lookback-years", type=int, default=5, help="award publish window")
    parser.add_argument("--horizon", type=int, default=270, help="expiry horizon in days")
    args = parser.parse_args()

    firm = load_profile(args.profile)
    print(f"Scanning award notices for {firm.name} (expiring within {args.horizon} days)...")
    records = fetch_expiries(firm, lookback_years=args.lookback_years, horizon_days=args.horizon)
    added = save_expiries(args.profile, records)
    print(
        f"  {len(records)} expiring contracts matched, {added} new -> {expiries_path(args.profile)}"
    )
    for r in records[:8]:
        print(
            f"  - ends {r['contract_end']}: {r['title'][:55]} | {r['buyer'][:30]} | holder: {r['incumbent'][:28]}"
        )


if __name__ == "__main__":
    main()
