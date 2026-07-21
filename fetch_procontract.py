"""Scrape ProContract / Due North (https://procontract.due-north.com) -- a public,
no-login eSourcing portal used by dozens of UK councils, NHS trusts, and (crucially)
schools/academies. Under the Procurement Act 2023, schools/academies are EXEMPT from
central publication on Find a Tender / Contracts Finder, so a meaningful slice of
school & council cleaning/security/catering/FM tenders live ONLY here. FTS/CF-only
competitors (TenderLead, Stotles, BidStats) cannot see these -- this scraper is a
differentiator, not a nice-to-have.

The Opportunities/Index list is server-rendered HTML behind a small amount of JS,
paged 10-rows-per-page via ?Page=N&PageSize=10 (PageSize is ignored server-side --
confirmed by probing with PageSize=100, still returns 10 rows). No login wall, no
Cloudflare/anti-bot challenge encountered during probing (2026-07-13). List columns:
Title (anchor text; the anchor's title="" attribute carries the reference number,
e.g. "DN820956"; href is the /Advert?advertId=<guid> detail page), Buyer,
Expression Start, Expression End, Estimated value. "Expression End" is the
close-of-interest deadline (confirmed against the detail page's "Expression of
interest window" field) -- DD/MM/YYYY on the list.

We deliberately do NOT click into every detail page: at ~640 live opportunities
across ~64 pages, one extra page load per row would be slow and impolite for a
daily job. Sector filtering runs on title+buyer text only (see _is_relevant on
this module), same fallback fetch.py itself uses via ALL_KEYWORDS.

Usage:
    uv run python fetch_procontract.py                # scrape + filter + save
    uv run python fetch_procontract.py --max-pages 5   # narrower run for testing
    uv run python fetch_procontract.py --all-sectors   # skip sector filter (debug)

Fails loud (raises) rather than writing empty/garbage data if the page structure
has changed, a login wall appears, or zero rows are found on page 1.
"""

import argparse
from datetime import datetime
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from fetch import ALL_KEYWORDS, save

BASE_URL = "https://procontract.due-north.com"
LIST_URL = BASE_URL + "/Opportunities/Index"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Sanity cap. ~64 pages exist as of 2026-07-13; this leaves headroom for growth
# while still bounding a daily run if the portal's listing balloons unexpectedly.
DEFAULT_MAX_PAGES = 80
GRID_SELECTOR = "#opportunitiesGrid"
NO_LOGIN_MARKERS = ("Sign in", "Log in", "Please log in", "Username", "Password")


def _parse_date_ddmmyyyy(text: str) -> str:
    """'22/07/2026' -> '2026-07-22'. Returns '' if unparseable (e.g. 'N/A', blank)
    -- build_leads.py already routes empty deadline_hint to the unverified tier,
    so we never invent a date here."""
    text = (text or "").strip()
    try:
        return datetime.strptime(text, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return ""


def _is_relevant(title: str, buyer: str) -> bool:
    """Same keyword fallback fetch.py uses for feeds with no CPV codes -- ProContract's
    list page gives us no CPV classification, only title+buyer text."""
    blob = f"{title} {buyer}".lower()
    return any(k in blob for k in ALL_KEYWORDS)


def _row_to_record(row) -> dict | None:
    """One <tr> from #opportunitiesGrid tbody -> flat record matching fetch.py's
    _normalize() shape. None if the row is the grid's own 'no data' placeholder
    (a single <td>) or otherwise malformed."""
    cells = row.locator("td")
    if cells.count() < 5:
        return None  # placeholder row ("There is no data available.") or malformed

    link = cells.nth(0).locator("a")
    if link.count() == 0:
        return None
    title = link.inner_text().strip()
    if not title:
        return None  # never write a record with an empty title

    ref = (link.get_attribute("title") or "").strip()
    href = link.get_attribute("href") or ""
    if not ref or not href:
        return None

    buyer = cells.nth(1).inner_text().strip() or "Unknown buyer"
    expression_end = cells.nth(3).inner_text().strip()
    value_hint = cells.nth(4).inner_text().strip()
    if value_hint.upper() == "N/A":
        value_hint = ""

    return {
        "id": f"procontract-{ref}",
        "title": title,
        "buyer": buyer,
        "url": urljoin(BASE_URL, href),
        "location": "",  # not present on the list page; would need a detail-page click
        "deadline_hint": _parse_date_ddmmyyyy(expression_end),
        "value_hint": value_hint,
        "description": "",  # not present on the list page (see module docstring)
    }


def scrape(max_pages: int = DEFAULT_MAX_PAGES) -> list[dict]:
    """Render each Opportunities/Index page in headless chromium and extract
    structured records. Raises RuntimeError loudly on a login wall, an unexpected
    DOM (grid missing / column count changed), or zero opportunities on page 1 --
    callers must not treat a raised error as "zero results, write nothing"."""
    records: list[dict] = []
    seen_refs: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        try:
            for page_num in range(1, max_pages + 1):
                url = (
                    f"{LIST_URL}?Page={page_num}&PageSize=10"
                    "&SortColumn=Title&SortDirection=Ascending"
                )
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(4000)  # let the grid's JS render finish

                body_text = page.inner_text("body")
                if any(marker in body_text for marker in NO_LOGIN_MARKERS):
                    raise RuntimeError(
                        "ProContract appears to require login (found "
                        f"{[m for m in NO_LOGIN_MARKERS if m in body_text]!r} in page "
                        f"text) -- stopping rather than scraping a login wall."
                    )

                grid = page.locator(GRID_SELECTOR)
                if grid.count() == 0:
                    raise RuntimeError(
                        f"#opportunitiesGrid not found on page {page_num} -- ProContract's "
                        "DOM has likely changed. Inspect the page manually before re-running."
                    )

                rows = grid.locator("tbody tr")
                row_count = rows.count()
                if page_num == 1 and row_count == 0:
                    raise RuntimeError(
                        "Zero rows found on page 1 of ProContract -- expected the "
                        "opportunities grid to render. Aborting rather than writing "
                        "empty data."
                    )

                page_records = []
                for i in range(row_count):
                    rec = _row_to_record(rows.nth(i))
                    if rec is None:
                        continue
                    if rec["id"] in seen_refs:
                        continue
                    seen_refs.add(rec["id"])
                    page_records.append(rec)

                if row_count > 0 and not page_records:
                    # Every row on this page was the "no data available" placeholder
                    # -> we've paged past the end of the listing.
                    break

                records.extend(page_records)
                print(f"  page {page_num}: {len(page_records)} opportunities")

                if row_count == 0:
                    break
        finally:
            browser.close()

    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--all-sectors", action="store_true")
    args = parser.parse_args()

    print("Scraping ProContract Opportunities/Index...")
    raw = scrape(args.max_pages)
    print(f"  scraped {len(raw)} total opportunities across all pages")

    if args.all_sectors:
        matched = raw
    else:
        matched = [r for r in raw if _is_relevant(r["title"], r["buyer"])]
    print(f"  {len(matched)} sector-relevant (of {len(raw)} scraped)")

    added = save(matched)
    print(f"  {added} new -> data/tenders-raw.jsonl")
    if matched:
        print(f"  sample: {matched[0]['title']}  ({matched[0]['buyer']})")


if __name__ == "__main__":
    main()
