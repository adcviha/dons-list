#!/usr/bin/env python3
"""
Don's List — Nanaimo rental scraper.
Scrapes Craigslist Nanaimo for private rental listings and writes JSON to docs/listings.json.
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

BASE_URL = "https://nanaimo.craigslist.org"
SEARCH_PATH = "/search/apa"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "listings.json"
REQUEST_DELAY = 2  # seconds between listing-page fetches

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
# Helpers
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
        (r"\b(\d+)\s*b(?:e)?d(?:r)?(?:oo)?m\b",  lambda m: f"{m[1]} Bedroom"),
        (r"\b(\d+)\s*br\b",                       lambda m: f"{m[1]} Bedroom"),
        (r"\b(\d+)\s*bdrm\b",                     lambda m: f"{m[1]} Bedroom"),
        (r"\b(\d+)\s*bed\b",                      lambda m: f"{m[1]} Bedroom"),
        (r"\bbachelor\b",                         lambda _: "Bachelor"),
        (r"\bstudio\b",                           lambda _: "Studio"),
        (r"\bshared\b",                           lambda _: "Shared"),
        (r"\b(\d+)\s*bedroom\b",                  lambda m: f"{m[1]} Bedroom"),
    ]:
        m = re.search(pat, t)
        if m:
            return fmt(m)
    return "Unknown"


def clean_price(raw: str) -> str:
    """Normalise price strings like '$1,700 / 1br' -> '$1,700'."""
    raw = raw.strip()
    m = re.match(r"\$[\d,]+(?:\.\d{2})?", raw)
    return m[0] if m else raw


# ---------------------------------------------------------------------------
# Search-results parsing
# ---------------------------------------------------------------------------

def parse_search_results(soup: BeautifulSoup) -> list[dict]:
    listings: list[dict] = []

    # Try several known Craigslist result-item selectors (newest first)
    selectors = [
        "li.cl-static-search-result",
        "li.result-row",
        "li[data-pid]",
        "div.result-info",
    ]

    items: list = []
    for sel in selectors:
        items = soup.select(sel)
        if items:
            log.info("Matched %d results using selector '%s'", len(items), sel)
            break

    if not items:
        # Last resort — find any <a> that looks like a listing link
        log.warning("No known result selectors matched; falling back to link scanning")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/apa/" in href or "/d/" in href:
                listings.append(_listing_from_link(a))
        return listings

    for item in items:
        listing = _parse_result_item(item)
        if listing:
            listings.append(listing)

    return listings


def _parse_result_item(item) -> dict | None:
    """Parse a single Craigslist result item."""
    # Title + link
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

    if link and not link.startswith("http"):
        link = urljoin(BASE_URL, link)

    # Price
    price_el = (
        item.find("span", class_="priceinfo")
        or item.find("span", class_="result-price")
        or item.find("div", class_="price")
    )
    price = clean_price(price_el.get_text(strip=True)) if price_el else "N/A"

    # Location
    loc_el = (
        item.find("div", class_="location")
        or item.find("span", class_="result-hood")
        or item.find("span", class_="nearby")
    )
    location = loc_el.get_text(strip=True) if loc_el else "Nanaimo"
    # Strip parentheses that Craigslist sometimes wraps around hoods
    location = location.strip("() ")

    return {
        "address": location,
        "size": parse_size_type(title),
        "price": price,
        "contact": "See listing",
        "link": link,
        "title": title,
    }


def _listing_from_link(a) -> dict | None:
    title = a.get_text(strip=True)
    link = a["href"]
    if not link.startswith("http"):
        link = urljoin(BASE_URL, link)
    if not title:
        return None
    return {
        "address": "Nanaimo",
        "size": parse_size_type(title),
        "price": "N/A",
        "contact": "See listing",
        "link": link,
        "title": title,
    }


# ---------------------------------------------------------------------------
# Individual listing pages (deep scrape)
# ---------------------------------------------------------------------------

def fetch_listing_details(url: str) -> dict:
    """Visit a single listing page and pull out address + contact info."""
    details: dict = {"address": "", "contact": ""}

    soup = fetch_page(url)
    if not soup:
        return details

    # --- Address ---
    # Map address block
    map_addr = soup.find("div", class_="mapaddress")
    if map_addr:
        details["address"] = map_addr.get_text(strip=True)

    # Sometimes address is in postinginfo div
    if not details["address"]:
        for attr_group in soup.select(".attrgroup span"):
            txt = attr_group.get_text(strip=True)
            # Heuristic: a string with a digit + street-type word
            if re.search(r"\d+\s+\w+\s+(St|Ave|Rd|Dr|Cres|Ct|Pl|Blvd|Way|Ln|Hwy)", txt):
                details["address"] = txt
                break

    # --- Contact ---
    body = soup.find("section", id="postingbody")
    body_text = body.get_text() if body else ""

    # Phone number in body
    phone_m = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", body_text)
    if phone_m:
        details["contact"] = phone_m.group(0)

    # Craigslist reply email relay
    if not details["contact"]:
        reply_btn = soup.find("button", class_="reply-button")
        if reply_btn:
            details["contact"] = "Reply via Craigslist"
        else:
            reply_link = soup.find("a", href=re.compile(r"mailto:|/reply/"))
            if reply_link:
                details["contact"] = "Reply via Craigslist"

    # Fallback
    if not details["contact"]:
        details["contact"] = "See listing"

    return details


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape() -> dict:
    log.info("=== Don's List scraper starting ===")

    search_url = f"{BASE_URL}{SEARCH_PATH}"
    log.info("Fetching search results: %s", search_url)

    soup = fetch_page(search_url)
    if not soup:
        return {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "listings": [],
            "error": "Failed to fetch Craigslist search page",
        }

    listings = parse_search_results(soup)
    log.info("Found %d raw listings", len(listings))

    # Deep-scrape each listing for address & contact
    for i, listing in enumerate(listings):
        if not listing["link"]:
            continue
        log.info("[%d/%d] %s", i + 1, len(listings), listing["title"][:60])
        time.sleep(REQUEST_DELAY)
        details = fetch_listing_details(listing["link"])
        if details.get("address"):
            listing["address"] = details["address"]
        if details.get("contact"):
            listing["contact"] = details["contact"]

    # Deduplicate by link
    seen = set()
    deduped: list[dict] = []
    for l in listings:
        if l["link"] and l["link"] not in seen:
            seen.add(l["link"])
            deduped.append(l)
        elif not l["link"]:
            deduped.append(l)

    log.info("Final count after dedup: %d", len(deduped))
    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "listings": deduped,
    }


def main() -> None:
    data = scrape()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    log.info("Wrote %d listings to %s", len(data["listings"]), OUTPUT_FILE)

    if not data["listings"] and "error" not in data:
        log.warning("Zero listings — Craigslist HTML may have changed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
