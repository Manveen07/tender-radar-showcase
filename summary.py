"""Daily Tender Radar health summary.

This is designed for GitHub Actions after `fetch.py` + `runner.py --all` run. It
reads local JSONL/state/output files and writes a small Markdown report so the
operator can see whether the morning scraper/classifier produced useful output
without opening Actions logs.

Usage:
    uv run python summary.py
    uv run python summary.py --out reports/daily-summary.md
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path("data")
OUT_DIR = Path("out")
REPORTS_DIR = Path("reports")
PROFILES_DIR = Path("profiles")
DEFAULT_PROFILE = "cleaning"


def _list_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))


def _outputs_path(profile_name: str) -> Path:
    if profile_name == DEFAULT_PROFILE:
        return DATA_DIR / "tender-outputs.jsonl"
    return DATA_DIR / f"tender-outputs-{profile_name}.jsonl"


def _seen_path(profile_name: str) -> Path:
    return DATA_DIR / f"seen-{profile_name}.json"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _read_json_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {str(x) for x in data}
    return set()


def _profile_counts(profile_name: str) -> dict[str, object]:
    rows = _read_jsonl(_outputs_path(profile_name))
    verdicts = Counter(str(r.get("bid_recommendation", "unknown")) for r in rows)
    seen = _read_json_set(_seen_path(profile_name))
    matched = [r for r in rows if r.get("bid_recommendation") in {"bid", "maybe"}]
    unsent = [r for r in matched if str(r.get("id")) not in seen]
    digest_files = sorted(OUT_DIR.glob(f"digest-{profile_name}-*.html"))
    return {
        "profile": profile_name,
        "classified": len(rows),
        "bid": verdicts.get("bid", 0),
        "maybe": verdicts.get("maybe", 0),
        "skip": verdicts.get("skip", 0),
        "unknown": sum(v for k, v in verdicts.items() if k not in {"bid", "maybe", "skip"}),
        "matched": len(matched),
        "unsent_matches": len(unsent),
        "seen_matches": len(seen),
        "digest_files": len(digest_files),
        "latest_digest": str(digest_files[-1]) if digest_files else "",
    }


def _expiries_counts(profile_name: str) -> tuple[int, int]:
    expiries = _read_jsonl(DATA_DIR / f"expiries-{profile_name}.jsonl")
    seen = _read_json_set(DATA_DIR / f"seen-expiries-{profile_name}.json")
    unseen = [e for e in expiries if str(e.get("id")) not in seen]
    return len(expiries), len(unseen)


def build_summary() -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    profiles = _list_profiles() or [DEFAULT_PROFILE]
    raw_tenders = _read_jsonl(DATA_DIR / "tenders-raw.jsonl")

    lines = [
        "# Tender Radar Daily Summary",
        "",
        f"Generated: `{now}`",
        "",
        "## Source fetch",
        "",
        f"- Raw tender records in `data/tenders-raw.jsonl`: **{len(raw_tenders)}**",
    ]
    if raw_tenders:
        latest_sample = raw_tenders[-1]
        lines.append(
            "- Latest stored sample: "
            f"{latest_sample.get('title', 'untitled')} — {latest_sample.get('buyer', 'unknown buyer')}"
        )

    lines += [
        "",
        "## Profile results",
        "",
        "| Profile | Classified | Bid | Maybe | Skip | Unsent matches | Expiries / unseen | Latest digest |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    any_warning = False
    for profile_name in profiles:
        counts = _profile_counts(profile_name)
        expiry_total, expiry_unseen = _expiries_counts(profile_name)
        latest_digest = counts["latest_digest"] or "—"
        lines.append(
            f"| {profile_name} | {counts['classified']} | {counts['bid']} | "
            f"{counts['maybe']} | {counts['skip']} | {counts['unsent_matches']} | "
            f"{expiry_total} / {expiry_unseen} | `{latest_digest}` |"
        )
        if counts["classified"] == 0:
            any_warning = True

    lines += ["", "## Operator action", ""]
    if any_warning:
        lines.append("- ⚠️ At least one profile has no classified outputs. Check the Actions logs and `GEMINI_API_KEY` secret.")
    else:
        lines.append("- ✅ Classifier outputs exist for all profiles.")
    lines.append("- If a prospect replied yes, run `fit_report.py --profile <profile> --company '<company>'` before replying.")
    lines.append("- Do not add more cold volume until reply/bounce data is reviewed.")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="reports/daily-summary.md")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_summary(), encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
