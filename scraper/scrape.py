#!/usr/bin/env python3
"""
Don's List — Nanaimo rental scraper (multi-source).
Scrapes Craigslist + Kijiji for private rental listings and writes JSON to docs/listings.json.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "listings.json"
REQUEST_DELAY = 2  # seconds between deep-scrape page fetches
MAX_PRICE = 1500   # dollars — listings above this are excluded

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


def parse_size_type(title: str) -> str:
    t = title.lower()
    for pat, fmt in [
        (r"\b(\d+)\s*b(?:e)?d(?:r)?(?:oo)?m\b", lambda m: f"{m[1]} Bedroom"),
        (r"\b(\d+)\s*br\b",                      lambda m: f"{m[1]} Bedroom"),
        (r"\b(\d+)\s*bdrm\b",                    lambda m: f"{m[1]} Bedroom"),
        (r"\b(\d+)\s*bed\b",                     lambda m: f"{m[1]} Bedroom"),
        (r"\b(\d+)\s*bedroom\b",                 lambda m: f"{m[1]} Bedroom"),
        (r"\bbachelor\b",                        lambda _: "Bachelor"),
        (r"\bstudio\b",                          lambda _: "Studio"),
        (r"\bshared\b",                          lambda _: "Shared"),
    ]:
        m = re.search(pat, t)
        if m:
            return fmt(m)
    return "Unknown"


def clean_price(raw: str) -> str:
    """Normalise a price string like '$1,700 / 1br' → '$1,700'."""
    raw = raw.strip().replace(" ", " ")
    m = re.match(r"\$[\d,]+(?:\.\d{2})?", raw)
    return m[0] if m else raw


def parse_price_value(raw: str) -> float | None:
    """Parse a price string into a numeric dollar amount.  Returns None if unparseable."""
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{2})?)", str(raw).replace(",", ""))
    if not m:
        return None
    try:
        return float(m[1])
    except ValueError:
        return None


def price_ok(raw: str, limit: int = MAX_PRICE) -> bool:
    """Return True if the listing price is at-or-below *limit*, or unparseable (keep unknown)."""
    v = parse_price_value(raw)
    if v is None:
        return True   # keep listings whose price we couldn't parse
    return v <= limit


def extract_phone(text: str) -> str | None:
    m = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    return m.group(0) if m else None


def make_abs(url: str, base: str) -> str:
    if not url:
        return ""
    return url if url.startswith("http") else urljoin(base, url)


# ---------------------------------------------------------------------------
# Craigslist
# ---------------------------------------------------------------------------

CL_BASE = "https://nanaimo.craigslist.org"
CL_SEARCH = CL_BASE + "/search/apa"


def scrape_craigslist() -> list[dict]:
    log.info("--- Craigslist ---")
    soup = fetch_page(CL_SEARCH)
    if not soup:
        return []

    # Try several known Craigslist result-item selectors
    items: list = []
    for sel in ["li.cl-static-search-result", "li.result-row", "li[data-pid]", "div.result-info"]:
        items = soup.select(sel)
        if items:
            log.info("Matched %d results using '%s'", len(items), sel)
            break

    if not items:
        log.warning("No known Craigslist selectors matched; scanning links")
        items = [a for a in soup.find_all("a", href=True) if "/apa/" in a["href"] or "/d/" in a["href"]]
        raw = [_cl_from_link(a) for a in items]
    else:
        raw = [r for item in items if (r := _cl_parse_item(item))]

    log.info("Craigslist raw: %d listings", len(raw))

    # Deep-scrape each for address + contact
    for i, listing in enumerate(raw):
        if not listing["link"]:
            continue
        log.info("[CL %d/%d] %s", i + 1, len(raw), listing["title"][:60])
        time.sleep(REQUEST_DELAY)
        details = _cl_fetch_details(listing["link"])
        if details.get("address"):
            listing["address"] = details["address"]
        if details.get("contact") and details["contact"] != "See listing":
            listing["contact"] = details["contact"]

    # Deduplicate by link
    seen = set()
    deduped = []
    for l in raw:
        if l["link"] and l["link"] not in seen:
            seen.add(l["link"])
            deduped.append(l)
        elif not l["link"]:
            deduped.append(l)
    log.info("Craigslist after dedup: %d", len(deduped))
    return deduped


def _cl_parse_item(item) -> dict | None:
    title_el = item.find("div", class_="title") or item.find("a", class_="result-title")
    if not title_el:
        a = item.find("a", href=True)
        title_el = a if a else None
    if not title_el:
        return None

    title = title_el.get_text(strip=True)
    link = ""
    if title_el.name == "a" and title_el.get("href"):
        link = title_el["href"]
    else:
        a = item.find("a", href=True)
        if a:
            link = a["href"]
    link = make_abs(link, CL_BASE)

    price_el = (
        item.find("span", class_="priceinfo")
        or item.find("span", class_="result-price")
        or item.find("div", class_="price")
    )
    price = clean_price(price_el.get_text(strip=True)) if price_el else "N/A"

    loc_el = (
        item.find("div", class_="location")
        or item.find("span", class_="result-hood")
        or item.find("span", class_="nearby")
    )
    location = loc_el.get_text(strip=True) if loc_el else "Nanaimo"
    location = location.strip("() ")

    return {
        "address": location,
        "size": parse_size_type(title),
        "price": price,
        "contact": "See listing",
        "link": link,
        "title": title,
        "source": "Craigslist",
    }


def _cl_from_link(a) -> dict | None:
    title = a.get_text(strip=True)
    link = make_abs(a.get("href", ""), CL_BASE)
    if not title:
        return None
    return {
        "address": "Nanaimo",
        "size": parse_size_type(title),
        "price": "N/A",
        "contact": "See listing",
        "link": link,
        "title": title,
        "source": "Craigslist",
    }


def _cl_fetch_details(url: str) -> dict:
    details: dict = {"address": "", "contact": ""}
    soup = fetch_page(url)
    if not soup:
        return details

    # Address — map block
    map_addr = soup.find("div", class_="mapaddress")
    if map_addr:
        details["address"] = map_addr.get_text(strip=True)

    # Address — attrgroup spans
    if not details["address"]:
        for sp in soup.select(".attrgroup span"):
            txt = sp.get_text(strip=True)
            if re.search(r"\d+\s+\w+\s+(St|Ave|Rd|Dr|Cres|Ct|Pl|Blvd|Way|Ln|Hwy)", txt):
                details["address"] = txt
                break

    # Contact — phone in body
    body = soup.find("section", id="postingbody")
    body_text = body.get_text() if body else ""
    phone = extract_phone(body_text)
    if phone:
        details["contact"] = phone
    else:
        # Craigslist relay
        if soup.find("button", class_="reply-button") or soup.find("a", href=re.compile(r"mailto:|/reply/")):
            details["contact"] = "Reply via Craigslist"
    return details


# ---------------------------------------------------------------------------
# Kijiji (skipped — JS-rendered, see function docstring)
# ---------------------------------------------------------------------------


def scrape_kijiji() -> list[dict]:
    """Kijiji is fully client-side rendered (Next.js + Apollo GraphQL).
    Listings load via JavaScript API calls after page load — not present
    in the initial HTML.  BeautifulSoup cannot execute JS, so we skip it
    rather than burning time on a guaranteed empty parse."""
    log.info("--- Kijiji ---")
    log.warning("Kijiji is JS-rendered — skipping (requires Playwright/Selenium, too heavy for free CI)")
    return []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape() -> dict:
    log.info("=" * 55)
    log.info("Don's List — Nanaimo Rental Scraper")
    log.info("Price cutoff: ≤ $%d", MAX_PRICE)
    log.info("=" * 55)

    errors: list[str] = []
    all_listings: list[dict] = []

    # Craigslist
    try:
        all_listings.extend(scrape_craigslist())
    except Exception as exc:
        msg = f"Craigslist failed: {exc}"
        log.exception(msg)
        errors.append(msg)

    # Kijiji
    try:
        all_listings.extend(scrape_kijiji())
    except Exception as exc:
        msg = f"Kijiji failed: {exc}"
        log.exception(msg)
        errors.append(msg)

    # Price filter
    before = len(all_listings)
    filtered = [l for l in all_listings if price_ok(l["price"])]
    log.info("Price filter: %d → %d (kept ≤ $%d + unparseable)", before, len(filtered), MAX_PRICE)

    # Sort by price (ascending, unknown at bottom)
    def _sort_key(l: dict):
        v = parse_price_value(l["price"])
        return (v is None, v or 0)
    filtered.sort(key=_sort_key)

    error_msg = "; ".join(errors) if errors else ""
    log.info("Final: %d listings  errors: %s", len(filtered), error_msg or "none")

    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "listings": filtered,
        "error": error_msg,
        "max_price": MAX_PRICE,
    }


def main() -> None:
    data = scrape()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    log.info("Wrote %d listings to %s", len(data["listings"]), OUTPUT_FILE)

    if not data["listings"] and data["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
