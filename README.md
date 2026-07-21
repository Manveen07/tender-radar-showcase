# tender-radar

Production pipeline that finds UK public-sector tenders for facilities
firms (cleaning, security, catering, ventilation/ductwork) — including the
ones most alert tools structurally miss — classifies them with an LLM
against per-client profiles, and renders ready-to-send client digests.
Runs unattended on GitHub Actions daily.

Built and operated solo. This is the sanitized engine of a live service;
client data, profiles, and outreach live in a private repo.

## Why it exists

Under the UK Procurement Act 2023, schools and academies are exempt from
publishing tenders on the central government portal. Their contracts appear
only on eSourcing portals (ProContract/Due North and similar) that the
mainstream tender-alert tools do not read. This pipeline reads both.

## Architecture

```
                 ┌─ fetch.py ────────── FTS + Contracts Finder OCDS APIs
  daily CI ──────┼─ fetch_procontract.py  Playwright scrape of eSourcing (the moat)
                 ├─ runner.py ────────── LLM classification per client profile
                 ├─ build_leads.py ───── verified / unverified deadline tiers,
                 │                       cross-source dedup, sector tagging
                 ├─ fetch_awards.py ──── award notices → competitor wins (90d)
                 │                       + contracts expiring within 12 months
                 └─ digest_client.py ─── per-profile plain-text digest emails
```

Key engineering decisions, all learned from production incidents:

- **Never cite a dead tender.** Award notices are filtered from the live
  feed; a backfill (`backfill_deadlines.py`) distinguishes a real award
  from a live two-stage tender publishing its *estimated* award timeline —
  the bare phrase "Award decision date" once killed a live £677k tender.
- **Title-only sector matching with word boundaries** ("guard" must not
  fire inside "safeguarding"; description-scans mistagged IT tenders as
  security). A narrow description-scan exception exists for specialist
  scope ("kitchen extract duct cleaning") buried inside broad M&E
  contracts — phrases specific enough that they cannot false-positive.
- **Cross-source dedup**: the same tender published on both a central feed
  and an eSourcing portal carries different fields on each; records merge
  on normalised title+buyer, keeping the usable deadline and the publicly
  verifiable URL.
- **Calibrated eval before trust**: the classifier ships with a golden set
  (`eval.py`) — currently 100% binary accuracy, with FALSE SKIP (missed
  contract) tracked as the expensive error class.
- **Polite API citizenship**: paced pagination, 429 Retry-After honoured,
  no workarounds. ToS-restricted portals get a manual watchlist instead of
  a scraper.
- **Honest empty states**: a client's quiet week says "nothing this week"
  — no filler. Trust is the product.

## Stack

Python 3.11, httpx, Playwright, Pydantic-style structured LLM output
(Gemini), pytest (unit + regression suite grown from real incidents),
ruff + pre-commit, GitHub Actions (daily cron + weekly wide-window sweep),
uv.

## Run it

```bash
uv sync
uv run python fetch.py --days 7          # central feeds
uv run python fetch_procontract.py       # eSourcing portals (Playwright)
uv run python build_leads.py             # tiered leads CSVs -> out/
uv run python fetch_awards.py            # competitor wins + expiring contracts
uv run python digest_client.py profiles/cleaning.yaml   # a client digest
uv run pytest                            # unit + regression suite
```

The two profiles included are demonstration firms; real client profiles
follow the same schema (`match_sectors`, `match_regions`, contact fields).

Note: a handful of tests are integration tests against fetched data — run
a fetch first for the full suite; the pure-unit tests pass standalone.
