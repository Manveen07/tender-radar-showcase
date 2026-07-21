"""Recover deadlines for tenders whose raw deadline_hint is empty.

Two things can be true when deadline_hint is empty:
  1. The tender is AWARDED/CLOSED — the notice page shows an "Award decision
     date" section instead of a submission deadline. This is a kill signal,
     not a gap: mark it expired so it never reaches outreach copy again
     (this is exactly the Jul 7 Harris Federation failure mode).
  2. The tender is genuinely still open but FTS's OCDS feed didn't carry
     tenderPeriod.endDate (common for two-stage/Competitive Flexible
     procedures). The notice HTML has a real "Submission" section with a
     parseable date — recover it into deadline_hint.

Must run from a UK-friendly IP: notice HTML pages 403 from cloud/foreign
IPs (confirmed for GitHub Actions and general cloud egress) but load fine
locally. This is a LOCAL job, not part of the CI pipeline — run manually
or via Task Scheduler.

Also fixes a second, unrelated bug this script exposed: old raw records
saved before the _notice_url fix (2026-07-07) still carry the broken
ocds-<id> URL format instead of the numeric-slug format. The FTS
per-release endpoint (data/tenders-raw.jsonl id -> ocdsReleasePackages/<id>)
returns the real release.id, e.g. "061462-2026", which IS the correct
notice slug. We re-resolve and rewrite the url field alongside the
deadline so links in outreach copy actually work.

Usage:
    uv run python backfill_deadlines.py            # dry run, prints only
    uv run python backfill_deadlines.py --write     # writes tenders-raw.jsonl
"""

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAW_PATH = Path("data/tenders-raw.jsonl")
FTS_RELEASE_API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages/{}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# Priority-ordered, NOT a single alternation. A two-stage notice (e.g.
# Attain Academy, verified 2026-07-12) carries BOTH an "Enquiry deadline"
# (earlier, informational — asking a question) and a "Deadline for requests
# to participate" (later, the actual gate to enter the competition). A
# single regex with (a|b|c) alternation returns whichever phrase appears
# FIRST in the text, which was "Enquiry deadline" — the wrong, earlier date.
# We check patterns in this list in order and take the first pattern that
# matches ANYWHERE, not the first phrase that appears leftmost in the text.
DEADLINE_PATTERNS = [
    re.compile(
        r"Deadline for requests to participate\s+(\d{1,2} [A-Za-z]+ \d{4}(?:,?\s*\d{1,2}[:.]\d{2}\s*[ap]m)?)",
        re.I,
    ),
    re.compile(
        r"Deadline date for the receipt of tenders\s+(\d{1,2} [A-Za-z]+ \d{4}(?:,?\s*\d{1,2}[:.]\d{2}\s*[ap]m)?)",
        re.I,
    ),
    re.compile(
        r"Tender submission deadline\s+(\d{1,2} [A-Za-z]+ \d{4}(?:,?\s*\d{1,2}[:.]\d{2}\s*[ap]m)?)",
        re.I,
    ),
    re.compile(
        r"Enquiry deadline\s+(\d{1,2} [A-Za-z]+ \d{4}(?:,?\s*\d{1,2}[:.]\d{2}\s*[ap]m)?)",
        re.I,
    ),
]
# Two distinct award/closed signals found by manual inspection (Barnes
# Primary, Harris Federation, 2026-07-12 — both share this exact wording):
#   1. "Award decision date" — appears on notices that show the full award
#      timeline (decision date, standstill period, contract signing date).
#   2. "Submission type ... Suppliers <company name>" — FTS serves the
#      AWARD notice at the same URL slug as the original tender notice once
#      a supplier is chosen. The presence of a named company under a
#      "Suppliers"/"Supplier" heading directly following "Submission type"
#      means this is no longer an open competition, regardless of whether
#      "Award decision date" text is present. This is the more common
#      signal in practice — Barnes and Harris both hit this one, not #1.
#
# CRITICAL: "Award decision date" is NOT a kill signal on its own. A LIVE
# two-stage tender (verified ground truth: Attain Academy Partnership,
# 2026-07-12, £677k, RtP deadline 2 Aug) publishes its PLANNED award
# timeline up front — "Award decision date (estimated) 8 September 2026" —
# while the competition is still open. Firing on the bare phrase marked
# Attain expired (1970), and save() in fetch.py then dropped it from the
# raw store permanently. Only treat "Award decision date" as awarded when
# the date is neither "(estimated)" nor in the future. The captured date is
# validated against "now" in investigate(); the regex just captures it.
AWARD_SIGNAL = re.compile(
    r"Award decision date\s*(\(estimated\))?\s*(\d{1,2} [A-Za-z]+ \d{4})?",
    re.I,
)
SUPPLIER_NAMED_SIGNAL = re.compile(
    r"Submission type\s+(?:Tenders|Requests to participate)\s+.{0,400}?Suppliers?\s+[A-Z]",
    re.I | re.DOTALL,
)


def _resolve_release_id(ocid: str) -> str | None:
    """Fetch the release, return its real numeric-slug id (e.g. 061462-2026)."""
    url = FTS_RELEASE_API.format(ocid)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            releases = data.get("releases", [])
            return releases[0].get("id") if releases else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            return None
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
            # TimeoutError: socket read timeouts surface directly (not wrapped
            # in URLError) and killed a full run on 2026-07-18 — treat like any
            # other transient fetch failure and move on.
            return None
    return None


def _fetch_notice_text(slug: str) -> str | None:
    """429s show up after a handful of rapid requests (same signature as
    PCS/CF's rate limiting elsewhere in this pipeline) — back off and retry
    rather than treating the first 429 as a permanent failure."""
    url = f"https://www.find-tender.service.gov.uk/Notice/{slug}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", "ignore")
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 15 * (attempt + 1)
                print(f"    429 — waiting {wait}s (attempt {attempt + 1}/4)")
                time.sleep(wait)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
    return None


def _parse_date(raw: str) -> str | None:
    raw = raw.strip().rstrip(".")
    for fmt in ("%d %B %Y, %I:%M%p", "%d %B %Y"):
        try:
            dt = datetime.strptime(raw.replace(",", ""), fmt.replace(",", ""))
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def investigate(record: dict) -> dict:
    """Returns {'action': 'expired'|'recovered'|'no-signal'|'fetch-failed',
    'deadline_hint': str|None, 'url': str|None}."""
    ocid = record["id"]
    slug = _resolve_release_id(ocid) if not ocid.startswith("0") else ocid
    slug = slug or ocid
    real_url = f"https://www.find-tender.service.gov.uk/Notice/{slug}"

    text = _fetch_notice_text(slug)
    if text is None:
        return {"action": "fetch-failed", "deadline_hint": None, "url": real_url}

    # Deadline text is the strongest positive signal — check first. A live
    # two-stage notice (verified ground truth: Attain Academy Partnership,
    # 2026-07-12) has "Deadline for requests to participate <date>" and no
    # named Supplier. Check this before the award signals so we never let
    # a real deadline get shadowed by an unrelated award-timeline mention
    # elsewhere on the page.
    for pattern in DEADLINE_PATTERNS:
        m = pattern.search(text)
        if m:
            parsed = _parse_date(m.group(1))
            if parsed:
                return {"action": "recovered", "deadline_hint": parsed, "url": real_url}

    # A named supplier under "Submission type" is unambiguous: the contract
    # is awarded. Kill it. (Barnes Primary, Harris Federation, 2026-07-12.)
    if SUPPLIER_NAMED_SIGNAL.search(text):
        return {"action": "expired", "deadline_hint": None, "url": real_url}

    # "Award decision date" is only a kill signal when the date is real,
    # concrete, and already passed. "(estimated) <future date>" is a LIVE
    # two-stage tender publishing its planned timeline (Attain Academy).
    am = AWARD_SIGNAL.search(text)
    if am:
        estimated = am.group(1) is not None
        award_date = _parse_date(am.group(2)) if am.group(2) else None
        past = award_date is not None and award_date < datetime.now(timezone.utc).isoformat()
        if not estimated and past:
            return {"action": "expired", "deadline_hint": None, "url": real_url}
        # estimated, future, or dateless "Award decision date" -> still open

    return {"action": "no-signal", "deadline_hint": None, "url": real_url}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write", action="store_true", help="write results back to tenders-raw.jsonl"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="cap how many to process (for testing)"
    )
    args = parser.parse_args()

    records = [
        json.loads(line)
        for line in RAW_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_id = {r["id"]: r for r in records}
    targets = [r for r in records if not r.get("deadline_hint")]
    if args.limit:
        targets = targets[: args.limit]

    print(f"{len(targets)} records with no deadline_hint to investigate\n")

    counts = {"expired": 0, "recovered": 0, "no-signal": 0, "fetch-failed": 0}
    for i, r in enumerate(targets, 1):
        result = investigate(r)
        counts[result["action"]] += 1
        print(f"[{i}/{len(targets)}] {result['action']:12} {r['title'][:55]}")

        if result["action"] == "expired":
            # deadline_hint stays empty; is_expired() in fetch.py can't act on
            # "no date" alone, so we set an explicit marker save() understands
            # as unambiguously in the past.
            by_id[r["id"]]["deadline_hint"] = "1970-01-01T00:00:00+00:00"
        elif result["action"] == "recovered":
            by_id[r["id"]]["deadline_hint"] = result["deadline_hint"]
        if result["url"]:
            by_id[r["id"]]["url"] = result["url"]

        time.sleep(5.0)  # polite pacing — two live requests per record (release
        # lookup + notice HTML), 2s wasn't enough and the site 429'd after ~6
        # records; 5s cleared it in later manual retries

    print(f"\n{counts}")

    if args.write:
        RAW_PATH.write_text("\n".join(json.dumps(v) for v in by_id.values()), encoding="utf-8")
        print(f"wrote {len(by_id)} records -> {RAW_PATH}")
    else:
        print("\ndry run — pass --write to save")


if __name__ == "__main__":
    main()
