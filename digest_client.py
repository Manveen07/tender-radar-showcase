"""Render a per-client weekly digest email from the live leads database.

This is the deliverable the £49/mo founding offer sells: one command turns a
client profile + tender-facts.csv into a ready-to-send plain-text email.
Honest by design -- if nothing matches, it says so instead of padding
(the Jul 7 stale-tender incident is why; trust is the product).

Matching is explicit, not clever: a tender is included when its sector is in
the profile's match_sectors AND a match_regions substring appears in its
buyer/title/location. eSourcing-only tenders (ProContract ids with no
central twin) are flagged -- they are the differentiator.

Usage:
    uv run python digest_client.py profiles/cleaning.yaml
    uv run python digest_client.py profiles/cleaning.yaml --out out/nigel-digest.txt
"""

import argparse
import csv
from datetime import date
from pathlib import Path

import yaml

LEADS_CSV = (
    Path(__file__).resolve().parent / "out" / "tender-facts.csv"
)


def load_profile(path: Path) -> dict:
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = [k for k in ("name", "match_sectors", "match_regions") if k not in profile]
    if missing:
        raise SystemExit(f"{path.name} is missing required fields: {missing}")
    return profile


def matching_tenders(profile: dict) -> list[dict]:
    sectors = set(profile["match_sectors"])
    regions = [r.lower() for r in profile["match_regions"]]
    rows = list(csv.DictReader(LEADS_CSV.open(encoding="utf-8")))
    matches = []
    for row in rows:
        if row["sector"] not in sectors:
            continue
        blob = f"{row['buyer']} {row['title']} {row['location']}".lower()
        if any(region in blob for region in regions):
            matches.append(row)
    matches.sort(key=lambda r: r["deadline"])
    return matches


def render(profile: dict, tenders: list[dict], today: date) -> str:
    first_name = profile.get("contact_first_name", "").strip()
    greeting = f"{first_name}," if first_name else f"{profile['name']} team,"
    lines = [greeting, ""]

    if not tenders:
        lines += [
            "Quiet week in your patch: nothing live right now that genuinely "
            "fits your sectors and counties. No filler from me -- you'll hear "
            "the moment something real lands.",
        ]
    else:
        lines += ["Live in your patch this week, closest deadline first:", ""]
        for t in tenders:
            days_left = (date.fromisoformat(t["deadline"]) - today).days
            moat = (
                "  [eSourcing portal only -- the standard alert tools cannot see this one]\n"
                if t["id"].startswith("procontract-")
                else ""
            )
            value = f" | {t['value']}" if t.get("value") else ""
            lines += [
                f"- {t['title']}",
                f"  {t['buyer']} | closes {t['deadline']} ({days_left} days){value}",
                f"{moat}  {t['url']}",
                "",
            ]
        lines += [
            "Want my read on whether any of these is worth your time before "
            "you spend effort on documents? Just reply with the one that "
            "interests you.",
        ]

    lines += ["", "Manveen"]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path, help="client profile YAML")
    parser.add_argument(
        "--out", type=Path, default=None, help="write digest here instead of stdout"
    )
    args = parser.parse_args()

    profile = load_profile(args.profile)
    tenders = matching_tenders(profile)
    digest = render(profile, tenders, date.today())

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(digest, encoding="utf-8")
        print(f"{len(tenders)} matches -> {args.out}")
    else:
        print(f"--- {profile['name']} | {len(tenders)} matches ---\n")
        print(digest)


if __name__ == "__main__":
    main()
