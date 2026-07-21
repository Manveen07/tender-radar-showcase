"""Tender Fit Radar runner — classify tenders via Gemini + Instructor.

Mirrors the leadlens runner: forced-reasoning Pydantic schema, cost/latency metrics,
idempotent jsonl save. The one addition is the firm profile — it's rendered into the
system prompt so the classifier scores each tender against a concrete capability sheet.

Multi-profile (A1): each firm lives in profiles/<name>.yaml. `--profile <name>` runs
one firm; `--all` runs every profile found in profiles/. With no flag at all, behavior
is unchanged from the original single-firm MVP (profile "cleaning", data/tender-outputs.jsonl).

Digest + dedup (A2/A3): after classifying, each profile's newly-matched ("bid"/"maybe")
tenders that haven't already been sent to it (tracked in data/seen-<profile>.json) are
handed to digest.py. `--dry-run` writes the HTML instead of sending it — see digest.py.

Usage:
    uv run python runner.py                       # classify all of data/tenders-raw.jsonl (cleaning profile)
    uv run python runner.py --in data/tenders-fixture.jsonl
    uv run python runner.py --idx 0               # just one (debug)
    uv run python runner.py --profile security --in data/tenders-fixture.jsonl
    uv run python runner.py --all --in data/tenders-fixture.jsonl --dry-run
"""

import argparse
import json
import os
import time
from pathlib import Path

import instructor
from dotenv import load_dotenv
from google import genai

import digest
import retender
from fetch import is_expired
from profile import DEFAULT_PROFILE, FirmProfile, list_profiles, load_profile
from tender import TenderFit

load_dotenv()

# Rolling alias, not a pinned version: gemini-2.5-flash was retired by Google
# on/around 2026-07-10 and every classify call started 404ing (the pinned name
# still appeared in ListModels while generation was cut off). The -latest alias
# tracks Google's current flash model so a retirement can't silently kill the
# daily pipeline again. Cost/latency metrics may shift when Google rolls it.
MODEL = "gemini-flash-latest"

# Gemini 2.5 Flash pricing (USD per 1M tokens, 2026) — same as leadlens
PRICE_INPUT_PER_M = 0.30
PRICE_OUTPUT_PER_M = 2.50


def system_prompt(firm: FirmProfile) -> str:
    return f"""You are a tender fit classifier for a small B2B service firm. Read each public
tender carefully and decide whether the firm should bid.

{firm.as_prompt_block()}

Rules:
- Quote specific phrases from the tender as evidence in fit_reasoning BEFORE choosing
  bid_recommendation. No verdict without quoted evidence.
- skip if the firm is disqualified: a mandatory certification it does not hold, security
  clearance it lacks, a delivery region it cannot serve, a turnover threshold above its
  floor, or a deadline already passed.
- If the deadline hint is empty, unknown, or requires a check, or if the tender has a two-stage procedure, you MUST NOT recommend 'bid'. Recommend 'maybe' at best and add 'manual deadline/liveness check needed' to missing_requirements.
- skip if the contract's scale clearly exceeds the firm's delivery capacity, even when the
  firm is formally eligible: judge from staff_count and annual_turnover. A contract whose
  likely annual value rivals or exceeds the firm's whole turnover, or that spans more sites
  than a team this size can staff (e.g. a national trust tendering a dozen-plus sites at
  once), is a skip — the cost of bidding outweighs the realistic chance of winning against
  larger firms. Exception: if the tender is explicitly split into lots, judge fit against
  the smallest single lot instead of the whole package, and treat it as maybe at best.
- maybe if it qualifies but a requirement needs human checking (a cert the profile doesn't
  confirm either way, ambiguous region, etc.). List the gap in missing_requirements.
- bid only when the firm clearly meets the stated mandatory requirements.
- Do NOT assume capabilities the firm profile does not list. Absence = the firm lacks it."""


def build_user_prompt(t: dict) -> str:
    return f"""Classify this tender:

ID: {t["id"]}
Title: {t["title"]}
Buyer: {t["buyer"]}
URL: {t["url"]}
Location hint: {t.get("location", "") or "not specified"}
Deadline hint: {t.get("deadline_hint", "") or "not specified"}
Value hint: {t.get("value_hint", "") or "not specified"}

Description / notice text:
{t.get("description", "") or "(no description text in notice)"}
"""


def load_tenders(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def make_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing — check .env file")
    return instructor.from_genai(genai.Client(api_key=api_key))


def classify(client, sys_prompt: str, t: dict) -> tuple[TenderFit, dict]:
    t0 = time.perf_counter()
    result, raw = client.chat.completions.create_with_completion(
        model=MODEL,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": build_user_prompt(t)},
        ],
        response_model=TenderFit,
    )
    latency_ms = (time.perf_counter() - t0) * 1000
    # echo inputs from source, not model
    result.id = t["id"]
    result.title = t["title"]
    result.buyer = t["buyer"]
    result.url = t["url"]

    usage = getattr(raw, "usage_metadata", None) or getattr(raw, "usage", None)
    in_tok = getattr(usage, "prompt_token_count", None) or getattr(usage, "input_tokens", 0) or 0
    out_tok = (
        getattr(usage, "candidates_token_count", None) or getattr(usage, "output_tokens", 0) or 0
    )
    cost_usd = (in_tok / 1e6) * PRICE_INPUT_PER_M + (out_tok / 1e6) * PRICE_OUTPUT_PER_M

    metrics = {
        "id": t["id"],
        "latency_ms": round(latency_ms, 1),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": round(cost_usd, 6),
    }
    return result, metrics


def _save(path: Path, key: str, record: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    existing = {}
    if path.exists():
        existing = {
            json.loads(line)[key]: json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    existing[record[key]] = record
    path.write_text("\n".join(json.dumps(v) for v in existing.values()), encoding="utf-8")


def outputs_path(profile_name: str) -> Path:
    # Default profile keeps the original filename so eval.py (and its default
    # --outputs data/tender-outputs.jsonl arg) needs no changes.
    if profile_name == DEFAULT_PROFILE:
        return Path("data/tender-outputs.jsonl")
    return Path(f"data/tender-outputs-{profile_name}.jsonl")


def metrics_path(profile_name: str) -> Path:
    if profile_name == DEFAULT_PROFILE:
        return Path("data/tender-metrics.jsonl")
    return Path(f"data/tender-metrics-{profile_name}.jsonl")


def seen_path(profile_name: str) -> Path:
    return Path(f"data/seen-{profile_name}.json")


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")))


def save_seen(path: Path, ids: set[str]) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def run_profile(
    profile_name: str,
    infile: Path,
    idx: int | None = None,
    dry_run: bool = True,
    reclassify: bool = False,
) -> None:
    print(f"\n===== profile: {profile_name} =====")
    firm = load_profile(profile_name)
    client = make_client()
    sys_prompt = system_prompt(firm)
    tenders = load_tenders(infile)
    targets = [tenders[idx]] if idx is not None else tenders

    out_path = outputs_path(profile_name)
    met_path = metrics_path(profile_name)

    # Incremental by default: skip tenders this profile already classified.
    # Before this (2026-07-10), every daily run re-classified the ENTIRE
    # database for every profile — with the DB now persisting and growing,
    # runtime and Gemini cost grew linearly forever (38-min CI runs). The
    # outputs file is id-keyed, so already-scored tenders are simply reused.
    # Pass --reclassify to force a full re-run (e.g. after a prompt change).
    if idx is None and not reclassify and out_path.exists():
        done_ids = {
            json.loads(line)["id"]
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        before = len(targets)
        targets = [t for t in targets if t["id"] not in done_ids]
        print(f"  incremental: {before - len(targets)} already classified, {len(targets)} new")

    totals = {"latency_ms": 0.0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "n": 0}
    counts = {"bid": 0, "maybe": 0, "skip": 0}

    for t in targets:
        print(f"\n--- {t['id']} {t['buyer']} — {t['title'][:60]} ---")
        try:
            result, metrics = classify(client, sys_prompt, t)
            print(f"  -> {result.bid_recommendation.upper()}: {result.summary}")
            if result.missing_requirements:
                print(f"     gaps: {', '.join(result.missing_requirements)}")
            print(f"  [metrics] {metrics['latency_ms']}ms  cost=${metrics['cost_usd']}")
            _save(out_path, "id", result.model_dump())
            _save(met_path, "id", metrics)
            counts[result.bid_recommendation] += 1
            for k in ("latency_ms", "input_tokens", "output_tokens", "cost_usd"):
                totals[k] += metrics[k]
            totals["n"] += 1
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")

    if totals["n"]:
        n = totals["n"]
        print(
            f"\n=== {n} tenders: {counts['bid']} bid / {counts['maybe']} maybe / {counts['skip']} skip ==="
        )
        print(
            f"  avg latency: {totals['latency_ms'] / n:.0f}ms   total cost: ${totals['cost_usd']:.4f}   per-tender: ${totals['cost_usd'] / n:.5f}"
        )

    # A3: idempotent digest — only tenders not already sent to this profile before.
    all_outputs = (
        [
            json.loads(line)
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if out_path.exists()
        else []
    )
    seen = load_seen(seen_path(profile_name))
    # Filter out any expired tenders or tenders with unknown/unverified deadlines from the digest
    valid_matches = []
    for t in all_outputs:
        if t["bid_recommendation"] not in ("bid", "maybe") or t["id"] in seen:
            continue
        deadline = t.get("deadline") or ""
        # Check for unknown/unverified deadlines
        if (
            not deadline
            or "unknown" in deadline.lower()
            or "manual" in deadline.lower()
            or "check" in deadline.lower()
        ):
            continue
        # Check if expired
        if is_expired(deadline):
            continue
        valid_matches.append(t)
    new_matches = valid_matches
    new_expiries = retender.load_new_expiries(profile_name)

    if not new_matches and not new_expiries:
        print(f"  digest: no new bid/maybe tenders or expiries for {profile_name} — skipped")
        return

    written = digest.deliver(
        profile_name, firm, new_matches, dry_run=dry_run, expiries=new_expiries
    )
    if written:
        print(f"  digest: wrote {written}")
    else:
        print(f"  digest: sent to {firm.recipient_email}")

    seen |= {t["id"] for t in new_matches}
    save_seen(seen_path(profile_name), seen)
    if new_expiries:
        retender.mark_expiries_seen(profile_name, new_expiries)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="infile", default="data/tenders-raw.jsonl")
    parser.add_argument("--idx", type=int, default=None, help="classify only this index")
    parser.add_argument(
        "--profile", default=DEFAULT_PROFILE, help="profile name (profiles/<name>.yaml)"
    )
    parser.add_argument("--all", action="store_true", help="run every profile in profiles/")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="write the digest to out/ instead of sending it (default: on)",
    )
    parser.add_argument(
        "--send", dest="dry_run", action="store_false", help="actually send the digest via SMTP"
    )
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="re-run the classifier on ALL tenders, not just unclassified ones (use after prompt/profile changes)",
    )
    args = parser.parse_args()

    infile = Path(args.infile)
    if not infile.exists():
        raise SystemExit(
            f"{infile} not found — run fetch.py first or use --in data/tenders-fixture.jsonl"
        )

    profile_names = list_profiles() if args.all else [args.profile]
    if not profile_names:
        raise SystemExit("no profiles found in profiles/")

    for name in profile_names:
        run_profile(name, infile, idx=args.idx, dry_run=args.dry_run, reclassify=args.reclassify)


if __name__ == "__main__":
    main()
