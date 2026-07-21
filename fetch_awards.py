"""Pull AWARD notices from FTS + Contracts Finder for the two outreach
experiments queued 2026-07-18:

  1. recent-wins.csv    -- competitor-win hooks: "X won the £Y contract in
                           your patch in May" (user rule: max 3 months old,
                           fresher stings more and the next tender is near).
  2. expiring-soon.csv  -- re-tender radar: contracts whose contractPeriod
                           ends within the next 12 months. Re-tenders
                           typically publish 3-4 months before expiry.

fetch.py deliberately SKIPS award releases (tender_tags_only=True) because
live outreach must never cite a dead tender. This module is the mirror
image: it keeps ONLY award releases, for sectors we serve.

Awards accumulate in data/awards-raw.jsonl (id-deduped merge, same pattern
as tenders-raw). The expiring radar gets better as the store grows; run
with --days 1095 once (overnight, politely rate-limited) to backfill three
years of history, then the daily/weekly cadence keeps it current.

Usage:
    uv run python fetch_awards.py               # 90-day pull + rebuild CSVs
    uv run python fetch_awards.py --days 1095   # deep historical backfill
"""

import argparse
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from fetch import CF_API, FTS_API, _get_page, _is_relevant_sector
from build_leads import classify_sector

AWARDS_RAW = Path("data/awards-raw.jsonl")
LEADS_DIR = Path(__file__).resolve().parent / "out"
OUT_WINS = LEADS_DIR / "recent-wins.csv"
OUT_EXPIRING = LEADS_DIR / "expiring-soon.csv"

WINS_WINDOW_DAYS = 90  # user rule 2026-07-18: 12 months is way too old
EXPIRY_HORIZON_DAYS = 365


def _normalize_award(pkg: dict) -> list[dict]:
    """One release can carry several awards (lots). Flatten each to a flat
    record; skip awards with no supplier name (nothing to cite)."""
    buyer = ((pkg.get("buyer") or {}).get("name")) or ""
    tender = pkg.get("tender") or {}
    title = tender.get("title") or ""
    ocid = pkg.get("ocid") or pkg.get("id") or ""
    out = []
    for aw in pkg.get("awards") or []:
        suppliers = [s.get("name", "") for s in (aw.get("suppliers") or []) if s.get("name")]
        if not suppliers:
            continue
        value = aw.get("value") or {}
        period = aw.get("contractPeriod") or {}
        out.append(
            {
                "id": f"{ocid}-{aw.get('id', '')}",
                "ocid": ocid,
                "buyer": buyer,
                "title": title or (aw.get("title") or ""),
                "winner": "; ".join(suppliers),
                "value": value.get("amount"),
                "currency": value.get("currency", ""),
                "award_date": (aw.get("date") or "")[:10],
                "contract_start": (period.get("startDate") or "")[:10],
                "contract_end": (period.get("endDate") or "")[:10],
            }
        )
    return out


def fetch_awards(days: int, max_pages: int = 50) -> list[dict]:
    since_fts = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    records: list[dict] = []
    with httpx.Client(
        timeout=30.0, headers={"Accept": "application/json"}, follow_redirects=True
    ) as client:
        for name, url, params in (
            ("FTS", FTS_API, {"updatedFrom": since_fts, "stages": "award", "limit": 100}),
            ("ContractsFinder", CF_API, {"publishedFrom": since_fts, "limit": 100}),
        ):
            page_url, page_params = url, params
            kept = 0
            for _ in range(max_pages):
                data = _get_page(client, page_url, page_params)
                if data is None:
                    break
                for pkg in data.get("releases", []):
                    if "award" not in (pkg.get("tag") or []):
                        continue
                    if not _is_relevant_sector(pkg):
                        continue
                    for rec in _normalize_award(pkg):
                        records.append(rec)
                        kept += 1
                page_url = (data.get("links", {}) or {}).get("next")
                page_params = None
                if not page_url:
                    break
                # Same politeness as fetch.py's _fetch_source. Omitting this
                # sleep (the original sin, 2026-07-19) made the API throw 429s
                # with ~120s penalties -- pacing at 2s/page is both politer
                # AND faster overall than eating penalty waits.
                time.sleep(2.0)
            print(f"  {name}: {kept} awards kept")
    return records


def merge_store(records: list[dict]) -> list[dict]:
    existing: dict[str, dict] = {}
    if AWARDS_RAW.exists():
        for line in AWARDS_RAW.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                existing[r["id"]] = r
    before = len(existing)
    for r in records:
        existing[r["id"]] = r
    AWARDS_RAW.parent.mkdir(exist_ok=True)
    AWARDS_RAW.write_text("\n".join(json.dumps(v) for v in existing.values()), encoding="utf-8")
    print(f"  store: {before} -> {len(existing)} awards")
    return list(existing.values())


def build_csvs(store: list[dict], today: datetime) -> None:
    wins_cutoff = (today - timedelta(days=WINS_WINDOW_DAYS)).strftime("%Y-%m-%d")
    expiry_max = (today + timedelta(days=EXPIRY_HORIZON_DAYS)).strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")

    for r in store:
        r["_sector"] = classify_sector(r.get("title", "")) or ""

    fields = [
        "sector",
        "buyer",
        "title",
        "winner",
        "value",
        "currency",
        "award_date",
        "contract_end",
        "ocid",
    ]

    def write(path: Path, rows: list[dict], sort_key: str) -> None:
        rows.sort(key=lambda r: r.get(sort_key) or "")
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(
                    {k: (r.get("_sector") if k == "sector" else r.get(k, "")) for k in fields}
                )
        print(f"  {len(rows)} -> {path}")

    wins = [r for r in store if r["_sector"] and r.get("award_date", "") >= wins_cutoff]
    expiring = [
        r for r in store if r["_sector"] and today_s <= (r.get("contract_end") or "") <= expiry_max
    ]
    write(OUT_WINS, wins, "award_date")
    write(OUT_EXPIRING, expiring, "contract_end")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=WINS_WINDOW_DAYS)
    parser.add_argument("--max-pages", type=int, default=50)
    args = parser.parse_args()

    print(f"Fetching award notices from the last {args.days} days...")
    records = fetch_awards(args.days, args.max_pages)
    store = merge_store(records)
    build_csvs(store, datetime.now(timezone.utc))


if __name__ == "__main__":
    main()
