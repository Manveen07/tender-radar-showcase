"""Build the leads database from raw tenders, filtering for live deadlines.

Sector tagging matches on TITLE ONLY, not the full description. A keyword
buried anywhere in a long description (e.g. "cyber security" in an IT
tender, "food hygiene" in a catering tender) used to mistag unrelated
notices as cleaning/security -- caught 2026-07-08 when Enterprise Backup
platforms, bus franchising, and medicines licensing all got tagged
"security"/"cleaning" off description-body matches. Title-only match is a
much tighter signal: buyers name the actual scope in the title.

Two output tiers:
  VERIFIED  -> tender-facts.csv: deadline_hint parsed from the raw OCDS feed,
               unambiguous, safe to quote directly in outreach.
  UNVERIFIED -> tender-facts-unverified.csv: sector-relevant but no parsed
               deadline anywhere (raw feed AND the LLM classifier both came
               up empty -- see runner.py's TenderFit.deadline, which already
               declines to guess rather than hallucinate a date). These are
               real, often high-value leads (Harris Federation, Barnes
               Primary, Stanmore -- the original good pitches all live
               here) but the deadline must be confirmed by opening the
               live notice before it goes in an email. Never auto-promote
               a row from this tier to the verified one.
"""

import json
import csv
import re
from datetime import datetime, timezone
from pathlib import Path

RAW = Path("data/tenders-raw.jsonl")
OUTPUTS = Path("data/tender-outputs.jsonl")
LEADS_DIR = Path(__file__).resolve().parent / "out"
OUT = LEADS_DIR / "tender-facts.csv"
OUT_UNVERIFIED = LEADS_DIR / "tender-facts-unverified.csv"

# Word-boundary patterns, not bare substrings -- "guard" as a substring
# matches inside "safeguarding", "surveillance" alone catches traffic-camera
# contracts, bare "hygiene" catches Legionella/water-compliance tenders that
# have nothing to do with a cleaning firm's actual service. Multi-word
# phrases stay as phrase matches (still anchored by \b on each side).
CLEANING_KEYWORDS = [
    r"clean(?:ing|er|ers)?",
    r"janitor(?:ial)?",
    r"window\s+clean",
    r"caretak(?:er|ing)",
    r"deep\s+clean",
]
SECURITY_KEYWORDS = [
    r"security",
    r"manned\s+guard",
    r"guarding",
    r"security\s+guard",
    r"patrol",
    r"cctv",
    r"keyhold(?:ing)?",
    r"concierge",
]
CATERING_KEYWORDS = [r"catering", r"canteen", r"kitchen"]
# A specialist client's niche (their words: kitchen duct, fire damper,
# anything duct, HVAC maintenance). Checked BEFORE cleaning so "duct
# cleaning" / "extract cleaning" tag hvac, not the generic cleaning bucket
# he explicitly does not want. Anchored phrases, no bare "duct" (matches
# "product") or bare "kitchen" (matches catering equipment).
HVAC_KEYWORDS = [
    r"kitchen\s+extract",
    r"extract\s+clean\w*",
    r"ductwork",
    r"duct\s+clean\w*",
    r"fire\s+damper\w*",
    r"ventilation\s+(?:clean\w*|maintenance|system\w*)",
    r"hvac",
    r"air\s+handling",
    r"grease\s+extract\w*",
    r"local\s+exhaust\s+ventilation",
    r"lev\s+test\w*",
    r"tr19",
    r"kitchen\s+exhaust",
]


def _matches_any(title_lower: str, patterns: list[str]) -> bool:
    return any(re.search(rf"\b{p}\b", title_lower) for p in patterns)


def _norm(s: str) -> str:
    """Collapse to comparable form: lowercase, alnum-only, single-spaced."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _usable_deadline(dl: str) -> bool:
    """A deadline_hint we can actually quote: present, not the 'unknown'
    placeholder, and not the 1970 expired-marker the backfill writes."""
    return bool(dl) and dl != "unknown" and not dl.startswith("1970")


def dedupe_cross_source(records: list[dict]) -> list[dict]:
    """The same public tender appears twice when it is published on BOTH
    central FTS/CF (id 'ocds-...') AND a ProContract eSourcing portal (id
    'procontract-...'). The two copies carry DIFFERENT fields: FTS often has
    'unknown'/empty deadline_hint for two-stage notices while ProContract
    carries the real 'Expression End' date, so the same live tender split
    into one dead-looking row and one live row (Cambridgeshire, Wythenshawe,
    Vale of White Horse -- 2026-07-14). Merge pairs sharing normalised
    title+buyer into one record: keep whichever copy has a usable deadline,
    prefer the FTS/CF notice URL (publicly verifiable, canonical) and fill
    any field the winner is missing from the other copy."""
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for r in records:
        key = (_norm(r.get("title", "")), _norm(r.get("buyer", "")))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    merged: list[dict] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            merged.append(group[0])
            continue

        # Winner = the copy with a usable deadline (prefer FTS on a tie so
        # its canonical URL survives); loser fills any gaps.
        def _rank(rec: dict) -> tuple[int, int]:
            has_dl = _usable_deadline(rec.get("deadline_hint", ""))
            is_fts = not rec["id"].startswith("procontract-")
            return (int(has_dl), int(is_fts))

        group_sorted = sorted(group, key=_rank, reverse=True)
        winner = dict(group_sorted[0])
        for other in group_sorted[1:]:
            if not _usable_deadline(winner.get("deadline_hint", "")) and _usable_deadline(
                other.get("deadline_hint", "")
            ):
                winner["deadline_hint"] = other["deadline_hint"]
            for field in ("value_hint", "location", "url"):
                if not winner.get(field) and other.get(field):
                    winner[field] = other[field]
        # Prefer the FTS/CF notice URL for the merged row even when the
        # ProContract copy won on deadline -- the central notice is publicly
        # verifiable without a portal account, so it is the safer link to
        # put in outreach.
        fts_url = next(
            (r["url"] for r in group if not r["id"].startswith("procontract-") and r.get("url")),
            None,
        )
        if fts_url:
            winner["url"] = fts_url
        merged.append(winner)
    return merged


def classify_sector(title: str) -> str | None:
    """Title-only, word-boundary match. Returns None (not a lead) if
    nothing fits -- the caller drops these instead of dumping them into a
    'facilities' catch-all bucket that isn't actually usable for outreach.

    Deliberately excludes bare 'hygiene' and 'surveillance' -- both fire
    on non-cleaning/non-security tenders (water-hygiene compliance,
    traffic-camera contracts) more often than on real leads."""
    title_lower = title.lower()
    # hvac first: "duct cleaning" is the specialist's work, not generic cleaning.
    if _matches_any(title_lower, HVAC_KEYWORDS):
        return "hvac"
    if _matches_any(title_lower, CLEANING_KEYWORDS):
        return "cleaning"
    if _matches_any(title_lower, SECURITY_KEYWORDS):
        return "security"
    if _matches_any(title_lower, CATERING_KEYWORDS):
        return "catering"
    return None


# HVAC-only description scan. the client's niche often hides in a broad M&E
# contract's scope, not the title -- West Suffolk Council's "Mechanical
# Services Maintenance Contract" (verified 2026-07-15) is titled generically
# but lists "Kitchen extract duct cleaning" and "Ventilation system duct
# cleaning" as line items. Title-only classify_sector drops it. We scan the
# description ONLY for these highly-specific multi-word phrases -- unlike the
# 2026-07-08 incident where bare "security"/"cleaning" in a description body
# mistagged IT/bus/medicine tenders, a phrase like "kitchen extract duct
# cleaning" cannot false-positive. Bare "hvac"/"air handling" are excluded
# from this list precisely because they DO appear in unrelated M&E notices.
HVAC_DESC_PHRASES = [
    r"kitchen\s+extract",
    r"extract\s+duct\s+clean\w*",
    r"duct\s+clean\w*",
    r"ductwork\s+clean\w*",
    r"fire\s+damper",
    r"ventilation\s+(?:system\s+)?duct\s+clean\w*",
    r"ventilation\s+clean\w*",
    r"grease\s+extract\w*",
    r"local\s+exhaust\s+ventilation",
    r"lev\s+test\w*",
    r"tr19",
    r"kitchen\s+exhaust",
]


def classify_hvac_by_description(description: str) -> bool:
    """True if the description names specialist duct/extract/ventilation
    cleaning work -- a client's niche buried in a broader contract's scope."""
    return _matches_any((description or "").lower(), HVAC_DESC_PHRASES)


def _load_classifier_notes(ids: set[str]) -> dict[str, dict]:
    """Read runner.py's classifier output (data/tender-outputs.jsonl) for
    extra context on unverified tenders -- estimated_value and the LLM's
    own fit_reasoning, which already declines to guess a deadline it can't
    find (see TenderFit.deadline: None if not found). We surface that
    reasoning, we don't override it."""
    notes: dict[str, dict] = {}
    if not OUTPUTS.exists():
        return notes
    with open(OUTPUTS, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("id") in ids:
                notes[d["id"]] = d
    return notes


def main() -> None:
    now = datetime.now(timezone.utc)
    records: list[dict] = []
    with open(RAW, encoding="utf-8") as f:
        for line in f:
            records.append(json.loads(line))

    before_dedup = len(records)
    records = dedupe_cross_source(records)
    if before_dedup != len(records):
        print(
            f"Deduped cross-source: {before_dedup} -> {len(records)} "
            f"({before_dedup - len(records)} FTS/ProContract pairs merged)"
        )

    # Filter: deadline in the future AND title-relevant to our sectors.
    # Anything sector-relevant but with no parsed deadline goes to the
    # UNVERIFIED tier instead of being silently dropped -- see module
    # docstring. It is never promoted to the verified list automatically.
    live: list[dict] = []
    unverified: list[dict] = []
    dropped_expired = 0
    dropped_irrelevant = 0
    for r in records:
        sector = classify_sector(r.get("title", ""))
        # Fallback for the client's niche: a broad M&E contract titled generically
        # (e.g. "Mechanical Services Maintenance") can still name kitchen
        # extract / duct / ventilation cleaning in its scope. Description
        # scan only fires on highly-specific phrases, so no false positives.
        if sector is None and classify_hvac_by_description(r.get("description", "")):
            sector = "hvac"
        if sector is None:
            dropped_irrelevant += 1
            continue
        r["_sector"] = sector

        dl = r.get("deadline_hint", "")
        parsed = None
        if dl:
            try:
                dt = datetime.fromisoformat(dl)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                parsed = dt
            except Exception:
                parsed = None

        if parsed is None:
            unverified.append(r)
        elif parsed <= now:
            dropped_expired += 1
        else:
            live.append(r)

    print(f"Total raw: {len(records)}")
    print(f"Dropped: {dropped_expired} expired, {dropped_irrelevant} title-irrelevant")
    print(f"Live + verified deadline: {len(live)}")
    print(f"Relevant but UNVERIFIED deadline: {len(unverified)}")

    # Sort by deadline
    live.sort(key=lambda r: r.get("deadline_hint", ""))

    # Write CSV
    fieldnames = ["id", "title", "buyer", "url", "location", "deadline", "value", "sector"]
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in live:
            w.writerow(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "buyer": r["buyer"],
                    "url": r.get("url", ""),
                    "location": r.get("location", "not stated"),
                    "deadline": r.get("deadline_hint", "")[:10],
                    "value": r.get("value_hint", "not stated"),
                    "sector": r["_sector"],
                }
            )

    print(f"\nWrote {len(live)} live tenders to {OUT}")
    print("\nBreakdown by sector:")
    sectors: dict[str, int] = {}
    for r in live:
        sectors[r["_sector"]] = sectors.get(r["_sector"], 0) + 1
    for s, c in sorted(sectors.items()):
        print(f"  {s}: {c}")

    print("\nAll live tenders:")
    for i, r in enumerate(live, 1):
        val = r.get("value_hint", "not stated")
        print(
            f"  {i}. [{r['_sector']}] {r['title'][:60]} | {r['buyer'][:30]} | "
            f"DL: {r.get('deadline_hint', '')[:10]} | {val}"
        )

    # Unverified tier -- write with an explicit warning column so nobody
    # mistakes this file for the verified one.
    unverified.sort(key=lambda r: r.get("title", ""))
    notes = _load_classifier_notes({r["id"] for r in unverified})
    unv_fieldnames = [
        "id",
        "title",
        "buyer",
        "url",
        "location",
        "value",
        "sector",
        "status",
        "classifier_note",
    ]
    with open(OUT_UNVERIFIED, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=unv_fieldnames)
        w.writeheader()
        for r in unverified:
            note = notes.get(r["id"], {})
            w.writerow(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "buyer": r["buyer"],
                    "url": r.get("url", ""),
                    "location": r.get("location", "not stated"),
                    "value": note.get("estimated_value") or r.get("value_hint", "not stated"),
                    "sector": r["_sector"],
                    "status": "DEADLINE NOT VERIFIED - open the notice before using in outreach",
                    "classifier_note": note.get("summary", ""),
                }
            )
    print(
        f"\nWrote {len(unverified)} sector-relevant tenders with unverified "
        f"deadlines to {OUT_UNVERIFIED}"
    )
    print("These are real leads but require opening the live notice to confirm")
    print("the deadline before use -- do not quote a deadline for these from memory.")


if __name__ == "__main__":
    main()
