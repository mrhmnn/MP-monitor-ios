"""
scraper.py

Fetches Marktplaats search result pages and extracts listing data.
Verified against the live site repeatedly (most recently 2026-07-13).

Strategy used:
  A) Marktplaats' internal LRP search API (/lrp/api/search) - the same
     JSON endpoint their frontend uses. Crucially, it's the only path
     that honors date-desc sorting, so it returns the genuinely NEWEST
     30 listings per query (verified live 2026-07-10).
  B) If that fails: embedded __NEXT_DATA__ JSON blob from the HTML search
     page (same item shape as A, but relevance-sorted).
  C) If that also fails: HTML/CSS selector parsing (SELECTORS below match
     on stable CSS-module prefixes; hash suffixes change per deploy).

Listing detail (VIP) pages are a separate path: fetch_listing_details()
for description/date/reserved-flag, fetch_listing_status() for bids and
gone-detection. Page fetches need browser-grade headers (_PAGE_HEADERS)
or Marktplaats serves a stripped variant without the description block.

Run `python scraper.py` directly (see bottom of file) to sanity-check
extraction against a live query.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# --- Fallback CSS selectors, verified against the live page on 2026-07-07 ---
# Marktplaats now uses CSS-module class names (e.g.
# "ListingTitle_hz-Listing-title-new__YIv8B") - the hash suffix changes on
# every frontend deploy, so we match on the stable module prefix with
# [class*=...]. The older selectors are kept behind them in each comma list
# as a second chance in case Marktplaats reverts or A/B-tests the old markup.
SELECTORS = {
    "listing_container": "li[class*='hz-Listing'], article[data-testid*='listing']",
    "title": "[class*='ListingTitle_hz-Listing-title'], [data-testid='listing-title'], h3, .hz-Listing-title",
    "price": "[class*='ListingPrice_hz-Listing-price'], [data-testid='listing-price'], .hz-Listing-price",
    "link": "a[href*='/v/']",
    "location": "[data-testid='location-label'], [data-testid='listing-location'], .hz-Listing-location",
    "description_snippet": "[class*='ListingDescription_hz-Listing-description'], [data-testid='listing-description'], .hz-Listing-description",
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
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    image_url: str = ""
    # Structured seller signals from the embedded JSON (search results only,
    # not available via the HTML fallback) - used by filters.py to reject
    # repair shops / professional sellers cheaply.
    seller_name: str = ""
    seller_has_website: bool = False
    # "NONE" for normal listings; "DAGTOPPER"/"TOPADVERTENTIE" are paid
    # promoted placements - private individuals dumping a broken phone
    # almost never pay to promote, repair shops constantly do.
    priority_product: str = "NONE"
    # Structured price data (JSON strategies only). price_text above is a
    # display string; these keep the raw numbers so market.py can build
    # price statistics instead of throwing the value away after formatting.
    price_cents: int = 0
    price_type: str = ""            # FIXED / MIN_BID / FAST_BID / ...
    condition: str = ""             # attributes[] "condition", e.g. "Zo goed als nieuw"
    storage_text: str = ""          # attributes[] "storage", e.g. "128 GB"

    @property
    def combined_text(self) -> str:
        return f"{self.title} {self.description_snippet}".lower()


# Dutch month abbreviations, for formatting __CONFIG__'s ISO "since"
# timestamp the same way Marktplaats' own "Sinds 8 jun '26" label does.
_NL_MONTHS = ("jan", "feb", "mrt", "apr", "mei", "jun",
              "jul", "aug", "sep", "okt", "nov", "dec")


@dataclass
class ListingDetails:
    """Everything worth extracting from one listing (VIP) page fetch."""
    description: str = ""    # description text - see fetch_listing_details docstring
    posted_date: str = ""    # "8 jun '26" - true original post date, not bump date
    is_reserved: bool = False


def fetch_listing_details(url: str, user_agent: str) -> ListingDetails:
    """
    One page fetch per matched/AI-reviewed listing, extracting:

    - description: from the server-rendered [data-collapsable='description']
      block. HONESTY NOTE (verified live 2026-07-13): Marktplaats truncates
      the description server-side at ~230 chars for anonymous requests -
      the "read more" button only toggles a CSS collapse, there is no
      endpoint with the rest. So this is slightly longer/cleaner than the
      ~200-char search-result snippet, but NOT guaranteed complete. The
      old get_full_listing_text() claimed to return the whole thing; it
      never could.
    - posted_date: from window.__CONFIG__ listing.stats.since (ISO
      timestamp) - the true original post date, unlike the search-result
      date which can be a paid bump. Structured data, replacing the old
      fragile "Sinds <date>" text regex.
    - is_reserved: __CONFIG__ listing.isReserved - seller marked it
      reserved for another buyer; worth a warning line in the alert.

    Returns a default ListingDetails on any failure - callers fall back
    to the search-result data they already have.
    """
    try:
        html = fetch_page(url, user_agent)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch listing page %s: %s", url, exc)
        return ListingDetails()

    details = ListingDetails()

    try:
        soup = BeautifulSoup(html, "html.parser")
        block = soup.select_one("[data-collapsable='description']") or soup.select_one(
            "div[class*='description']"
        )
        if block:
            details.description = block.get_text(" ", strip=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse description for %s: %s", url, exc)

    config_data = _parse_vip_config(html)
    if config_data:
        listing = config_data.get("listing", {})
        if isinstance(listing, dict):
            details.is_reserved = bool(listing.get("isReserved", False))
            since = listing.get("stats", {}).get("since", "") if isinstance(
                listing.get("stats"), dict
            ) else ""
            if since:
                try:
                    dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                    details.posted_date = (
                        f"{dt.day} {_NL_MONTHS[dt.month - 1]} '{dt.year % 100:02d}"
                    )
                except (ValueError, IndexError):
                    pass
    return details


@dataclass
class ListingStatus:
    """Result of a listing detail-page check (see fetch_listing_status)."""
    gone: bool = False
    # Bid amounts in cents, highest first. None = page fetched but no bids
    # array found (parse failure) - callers should treat that as "unknown",
    # not "zero bids". [] = bids array present and empty.
    bid_cents: Optional[list[int]] = None


# Text markers on the "this listing no longer exists" page. Marktplaats
# serves removed listings as a soft page rather than a clean 404 in some
# cases, so a 200 response alone doesn't prove the listing is still live.
_GONE_MARKERS = (
    "niet meer beschikbaar",
    "advertentie is verwijderd",
    "is helaas verwijderd",
)


def _find_bids(node) -> Optional[list]:
    """Recursively find the first "bids" list in the page-state JSON tree.
    The exact path has changed between frontend deploys before, so a key
    search is more robust than a hardcoded path."""
    if isinstance(node, dict):
        bids = node.get("bids")
        if isinstance(bids, list):
            return bids
        for value in node.values():
            found = _find_bids(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_bids(value)
            if found is not None:
                return found
    return None


# Listing detail (VIP) pages are NOT Next.js like the search page - their
# state lives in an inline `window.__CONFIG__ = {...}` script (verified
# live 2026-07-11). The bids sit under bidsInfo: {"bids": [{"value":
# <cents>, ...}, ...], "currentMinimumBid": ...}.
_VIP_CONFIG_RE = re.compile(r"window\.__CONFIG__\s*=\s*")


def _parse_vip_config(html: str) -> Optional[dict]:
    m = _VIP_CONFIG_RE.search(html)
    if not m:
        return None
    end = html.find("</script>", m.end())
    if end == -1:
        return None
    try:
        return json.loads(html[m.end():end].strip().rstrip(";"))
    except json.JSONDecodeError:
        return None


def fetch_listing_status(url: str, user_agent: str) -> ListingStatus:
    """
    Fetch a listing's own page and report (a) whether the listing is gone
    (sold/removed/expired) and (b) its current bids, from the embedded
    window.__CONFIG__ JSON (verified live 2026-07-11, e.g. m2419022529
    showed its €150/€160 bids there).

    Raises on network trouble so callers can distinguish "couldn't check"
    from "checked and gone" - a transient fetch error must never mark a
    listing as sold.
    """
    client = _get_http_client(user_agent, 15.0)
    resp = client.get(url, headers=_PAGE_HEADERS)
    if resp.status_code in (404, 410):
        return ListingStatus(gone=True)
    resp.raise_for_status()

    html = resp.text
    lowered = html.lower()
    if any(marker in lowered for marker in _GONE_MARKERS):
        return ListingStatus(gone=True)

    data = _parse_vip_config(html)
    if data is None:
        return ListingStatus(gone=False, bid_cents=None)

    bids = _find_bids(data)
    if bids is None:
        return ListingStatus(gone=False, bid_cents=None)
    amounts = sorted(
        (int(b["value"]) for b in bids if isinstance(b, dict) and isinstance(b.get("value"), (int, float))),
        reverse=True,
    )
    return ListingStatus(gone=False, bid_cents=amounts)


def build_search_url(base_url_template: str, query: str) -> str:
    return base_url_template.format(query=quote(query))


# One shared client per process: reuses TCP/TLS connections across the
# ~26 search queries plus per-match detail fetches in a scan, instead of
# a full handshake per request. The script is run-and-exit, so the OS
# cleans the connection up at the end - no explicit close needed.
_http_client: Optional[httpx.Client] = None


def _get_http_client(user_agent: str, timeout: float) -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            },
            timeout=timeout,
            follow_redirects=True,
        )
    return _http_client


# Extra headers for LISTING/SEARCH PAGE fetches (not the LRP API): without
# the Sec-Fetch-* / Accept set a real browser sends, Marktplaats serves a
# stripped page variant that omits the server-rendered description block
# entirely (verified live 2026-07-13 - same URL, with vs without these
# headers). The LRP API doesn't need them; a browser sends different
# Sec-Fetch values for XHR anyway.
_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_page(url: str, user_agent: str, timeout: float = 15.0) -> str:
    client = _get_http_client(user_agent, timeout)
    try:
        resp = client.get(url, headers=_PAGE_HEADERS)
        resp.raise_for_status()
        return resp.text
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        # One retry on transient failures (timeouts, connection resets,
        # 5xx). Without this, a single network hiccup silently costs the
        # whole query's 30 newest listings for that run. Client errors
        # like 404 won't be cured by retrying, so skip those.
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            raise
        logger.warning("Fetch failed for %s (%s), retrying once", url, exc)
        resp = client.get(url, headers=_PAGE_HEADERS)
        resp.raise_for_status()
        return resp.text


def _extract_listing_id_from_url(url: str) -> str:
    """
    Marktplaats listing URLs look like .../v/category/subcategory/m2409612663-some-title
    (verified live 2026-07-07). The m-prefixed ID is kept as-is so it's
    identical to the `itemId` the JSON strategy stores - if the two
    strategies produced different IDs for the same listing, a strategy
    switchover would re-alert on everything already seen.
    """
    match = re.search(r"/(m?\d{6,})-", url)
    if match:
        return match.group(1)
    # Fallback: just use the whole URL as the id if the pattern doesn't match
    return url


def _listing_from_item(item: dict) -> Listing:
    """
    Build a Listing from one Marktplaats listing-JSON item. The exact same
    item shape appears in two places (verified live 2026-07-10): the LRP
    search API response (`/lrp/api/search` -> "listings") and the
    __NEXT_DATA__ blob embedded in the HTML search page - so both
    strategies share this parser.
    """
    url = item.get("vipUrl", item.get("url", ""))
    if url and not url.startswith("http"):
        url = "https://www.marktplaats.nl" + url

    location = item.get("location", {}) if isinstance(item.get("location"), dict) else {}

    # Extract first image URL. Marktplaats provides multiple size
    # variants in the `pictures` array; the "large" one is a good
    # quality/size balance for Telegram (under Telegram's ~5MB
    # sendPhoto limit while still readable on a phone screen).
    image_url = ""
    pictures = item.get("pictures", [])
    if pictures and isinstance(pictures, list):
        first_pic = pictures[0]
        if isinstance(first_pic, dict):
            image_url = (
                first_pic.get("largeUrl")
                or first_pic.get("mediumUrl")
                or first_pic.get("extraExtraLargeUrl")
                or ""
            )
    # Fallback to imageUrls if pictures didn't yield anything
    if not image_url:
        image_urls = item.get("imageUrls", [])
        if image_urls and isinstance(image_urls, list):
            raw = image_urls[0]
            # imageUrls entries often start with "//" - add scheme
            if raw.startswith("//"):
                image_url = "https:" + raw
            elif raw.startswith("http"):
                image_url = raw

    seller = item.get("sellerInformation", {}) if isinstance(item.get("sellerInformation"), dict) else {}

    # condition/storage appear in both `attributes` and `extendedAttributes`
    # (verified live 2026-07-11); check both since either can be missing.
    attr_map: dict[str, str] = {}
    for attr_list in (item.get("attributes"), item.get("extendedAttributes")):
        if isinstance(attr_list, list):
            for attr in attr_list:
                if isinstance(attr, dict) and attr.get("key") and attr.get("value"):
                    attr_map.setdefault(attr["key"], attr["value"])

    price_info = item.get("priceInfo") if isinstance(item.get("priceInfo"), dict) else {}

    return Listing(
        listing_id=str(item.get("itemId", item.get("id", ""))),
        title=item.get("title", ""),
        description_snippet=item.get("description", ""),
        price_text=_format_price(item.get("priceInfo")),
        location_text=location.get("cityName", ""),
        url=url,
        posted_date=item.get("date", ""),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        image_url=image_url,
        seller_name=seller.get("sellerName", ""),
        seller_has_website=bool(seller.get("showWebsiteUrl", False)),
        priority_product=item.get("priorityProduct", "NONE") or "NONE",
        price_cents=int(price_info.get("priceCents") or 0),
        price_type=price_info.get("priceType", "") or "",
        condition=attr_map.get("condition", ""),
        storage_text=attr_map.get("storage", ""),
    )


# Marktplaats' internal search API - the same one their frontend calls when
# you change the sort order in the browser. Unlike the HTML search page
# (whose #Sort fragment never reaches the server, so it's always
# relevance-sorted), this endpoint honors sortBy/sortOrder and returns true
# newest-first results. No auth needed. Category IDs match the
# telecommunicatie / mobiele-telefoons-apple-iphone path baked into
# base_search_url. Verified live 2026-07-10.
LRP_API_URL = "https://www.marktplaats.nl/lrp/api/search"
LRP_L1_CATEGORY_ID = 820   # telecommunicatie
LRP_L2_CATEGORY_ID = 1953  # mobiele-telefoons-apple-iphone


def _fetch_listings_api(query: str, user_agent: str, timeout: float = 15.0) -> list[Listing]:
    """Strategy A: LRP search API, sorted newest-first."""
    params = {
        "query": query,
        "l1CategoryId": str(LRP_L1_CATEGORY_ID),
        "l2CategoryId": str(LRP_L2_CATEGORY_ID),
        "sortBy": "SORT_INDEX",
        "sortOrder": "DECREASING",
        "limit": "30",
        "offset": "0",
    }
    client = _get_http_client(user_agent, timeout)
    try:
        resp = client.get(LRP_API_URL, params=params)
        resp.raise_for_status()
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        # Same one-retry-on-transient-failure policy as fetch_page().
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            raise
        logger.warning("LRP API fetch failed for '%s' (%s), retrying once", query, exc)
        resp = client.get(LRP_API_URL, params=params)
        resp.raise_for_status()

    items = resp.json().get("listings", [])
    listings: list[Listing] = []
    for item in items:
        try:
            listings.append(_listing_from_item(item))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping malformed API listing item: %s", exc)
    return listings


def _parse_json_blob(html: str) -> list[Listing]:
    """
    Strategy B: look for an embedded JSON script tag with listing data in
    the HTML search page. NOTE: this page is relevance-sorted, not
    newest-first - it's only used when the LRP API call fails.
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
            listings.append(_listing_from_item(item))
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
    """Strategy C: plain CSS-selector based scraping. See SELECTORS above."""
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
    # Strategy A: LRP API - the only path that's genuinely newest-first.
    # The HTML strategies below are relevance-sorted (the #Sort fragment in
    # base_search_url never reaches the server), which silently misses
    # fresh listings that rank poorly on relevance - so they're kept
    # strictly as a fallback for when Marktplaats changes/blocks the API.
    try:
        listings = _fetch_listings_api(query, user_agent)
        if listings:
            logger.info("Extracted %d listings via LRP API (date-desc)", len(listings))
            return listings
        logger.warning("LRP API returned 0 listings for '%s', trying HTML fallback", query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LRP API failed for '%s' (%s), trying HTML fallback", query, exc)

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
