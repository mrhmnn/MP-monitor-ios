"""
scraper.py

Fetches Marktplaats search result pages and extracts listing data.

IMPORTANT - READ THIS BEFORE RUNNING:
I cannot verify Marktplaats' current page structure from my sandboxed
environment (marktplaats.nl isn't in my network allowlist for direct
testing here). This module is written defensively with TWO extraction
strategies, but you WILL likely need to verify/adjust the CSS selectors
in `_parse_html_fallback()` once against the live page:

  1. Open a Marktplaats search URL in Chrome
  2. Right-click a listing title -> "Inspect"
  3. Note the actual tag/class/data-testid attributes
  4. Update SELECTORS below to match

Strategy used:
  A) Try to find embedded JSON data in a <script> tag (many modern sites,
     including Marktplaats' Next.js-based frontend, embed a JSON blob with
     the full listing dataset - this is far more reliable than CSS
     selectors since it doesn't break when they restyle the page).
  B) If that fails, fall back to HTML/CSS parsing.

Run `python scraper.py` directly (see bottom of file) to sanity-check
extraction against a live URL before wiring it into main.py.
"""

import json
import logging
import re
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# --- Best-effort selectors - VERIFY THESE against the live page (see docstring above) ---
SELECTORS = {
    "listing_container": "li[class*='hz-Listing'], article[data-testid*='listing']",
    "title": "[data-testid='listing-title'], h3, .hz-Listing-title",
    "price": "[data-testid='listing-price'], .hz-Listing-price",
    "link": "a[href*='/v/']",
    "location": "[data-testid='listing-location'], .hz-Listing-location",
    "description_snippet": "[data-testid='listing-description'], .hz-Listing-description",
}


@dataclass
class Listing:
    listing_id: str
    title: str
    description_snippet: str
    price_text: str
    location_text: str
    url: str
    posted_date: str = ""
    latitude: float = None
    longitude: float = None

    @property
    def combined_text(self) -> str:
        return f"{self.title} {self.description_snippet}".lower()


def get_accurate_posting_date(url: str, user_agent: str) -> str:
    """
    The search-results 'date' field (used in Listing.posted_date) can
    reflect a paid bump/repost rather than the listing's true original
    post date - confirmed by comparing real listings where the two
    differed by over a week. The individual listing page shows a
    trustworthy "Sinds <date>" (Since <date>) field instead.

    Only call this for actual matches, not every scanned listing - it's
    one extra page fetch per listing, which is fine for ~10-25 matches a
    run but wasteful for the ~280 listings scanned before filtering.
    """
    try:
        html = fetch_page(url, user_agent)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        match = re.search(r"Sinds\s+(\d{1,2}\s+\w+\s*'?\d{2,4})", text)
        if match:
            return match.group(1)
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch accurate posting date for %s: %s", url, exc)
        return ""


def build_search_url(base_url_template: str, query: str) -> str:
    return base_url_template.format(query=quote(query))


def fetch_page(url: str, user_agent: str, timeout: float = 15.0) -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    }
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


def _extract_listing_id_from_url(url: str) -> str:
    """Marktplaats listing URLs look like .../v/category/12345-some-title.html"""
    match = re.search(r"/(\d{6,})-", url)
    if match:
        return match.group(1)
    # Fallback: just use the whole URL as the id if the pattern doesn't match
    return url


def _parse_json_blob(html: str) -> list[Listing]:
    """
    Strategy A: look for an embedded JSON script tag with listing data.
    This is a best-effort attempt - the exact script id/shape may have
    changed by the time you run this. If it returns an empty list, the
    HTML fallback below will be used instead.
    """
    listings: list[Listing] = []
    soup = BeautifulSoup(html, "html.parser")

    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        return listings

    try:
        data = json.loads(script_tag.string)
    except json.JSONDecodeError:
        logger.debug("Found __NEXT_DATA__ but couldn't parse as JSON")
        return listings

    # NOTE: the exact path into this dict WILL need adjustment - this is a
    # placeholder based on typical Next.js listing-page shapes. Print
    # `data` (or dump to a file) the first time you run this to find the
    # real path to the listings array.
    try:
        search_results = (
            data.get("props", {})
            .get("pageProps", {})
            .get("searchRequestAndResponse", {})
            .get("listings", [])
        )
    except AttributeError:
        search_results = []

    for item in search_results:
        try:
            url = item.get("vipUrl", item.get("url", ""))
            if url and not url.startswith("http"):
                url = "https://www.marktplaats.nl" + url

            location = item.get("location", {}) if isinstance(item.get("location"), dict) else {}

            listings.append(
                Listing(
                    listing_id=str(item.get("itemId", item.get("id", ""))),
                    title=item.get("title", ""),
                    description_snippet=item.get("description", ""),
                    price_text=_format_price(item.get("priceInfo")),
                    location_text=location.get("cityName", ""),
                    url=url,
                    posted_date=item.get("date", ""),
                    latitude=location.get("latitude"),
                    longitude=location.get("longitude"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping malformed JSON listing item: %s", exc)

    return listings


def _format_price(price_info) -> str:
    """
    Marktplaats' priceInfo looks like:
      {"priceCents": 12500, "priceType": "FIXED", ...}
      {"priceCents": 0, "priceType": "SEE_DESCRIPTION", ...}
      {"priceCents": 5000, "priceType": "MIN_BID", ...}
      {"priceCents": 0, "priceType": "FAST_BID", ...}  (this is "bieden")
    """
    if not isinstance(price_info, dict):
        return "Bieden"

    price_type = price_info.get("priceType", "")
    cents = price_info.get("priceCents", 0)
    euros = cents / 100 if cents else 0

    if price_type == "FIXED":
        return f"€{euros:.0f}"
    if price_type == "MIN_BID":
        return f"Bieden vanaf €{euros:.0f}"
    if price_type in ("FAST_BID", "SEE_DESCRIPTION", "NOTK"):
        return "Bieden"
    return f"€{euros:.0f}" if euros else "Bieden"


def _parse_html_fallback(html: str) -> list[Listing]:
    """Strategy B: plain CSS-selector based scraping. See SELECTORS above."""
    listings: list[Listing] = []
    soup = BeautifulSoup(html, "html.parser")

    containers = soup.select(SELECTORS["listing_container"])
    logger.info("HTML fallback found %d candidate listing containers", len(containers))

    for container in containers:
        title_el = container.select_one(SELECTORS["title"])
        link_el = container.select_one(SELECTORS["link"])
        price_el = container.select_one(SELECTORS["price"])
        location_el = container.select_one(SELECTORS["location"])
        desc_el = container.select_one(SELECTORS["description_snippet"])

        if not title_el or not link_el:
            continue  # not a real listing card, skip

        url = link_el.get("href", "")
        if url.startswith("/"):
            url = "https://www.marktplaats.nl" + url

        listings.append(
            Listing(
                listing_id=_extract_listing_id_from_url(url),
                title=title_el.get_text(strip=True),
                description_snippet=desc_el.get_text(strip=True) if desc_el else "",
                price_text=price_el.get_text(strip=True) if price_el else "",
                location_text=location_el.get_text(strip=True) if location_el else "",
                url=url,
            )
        )

    return listings


def fetch_listings(query: str, base_url_template: str, user_agent: str) -> list[Listing]:
    url = build_search_url(base_url_template, query)
    logger.info("Fetching: %s", url)
    html = fetch_page(url, user_agent)

    listings = _parse_json_blob(html)
    if listings:
        logger.info("Extracted %d listings via JSON blob strategy", len(listings))
        return listings

    listings = _parse_html_fallback(html)
    logger.info("Extracted %d listings via HTML fallback strategy", len(listings))
    if not listings:
        logger.warning(
            "No listings extracted for query '%s'. The page structure has likely "
            "changed - see the docstring at the top of scraper.py for how to fix this.",
            query,
        )
    return listings


if __name__ == "__main__":
    # Quick manual sanity check: run `python scraper.py` to test extraction
    # against a live query before wiring everything together in main.py.
    logging.basicConfig(level=logging.INFO)
    test_query = "iphone scherm kapot"
    test_base_url = "https://www.marktplaats.nl/l/telecommunicatie/mobiele-telefoons-apple-iphone/q/{query}/#Sort:SortIndex|dateDesc"
    test_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

    results = fetch_listings(test_query, test_base_url, test_ua)
    print(f"\nFound {len(results)} listings:\n")
    for r in results[:5]:
        print(f"- {r.title} | {r.price_text} | {r.posted_date} | ({r.latitude}, {r.longitude}) | {r.url}")
