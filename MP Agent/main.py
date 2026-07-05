"""
main.py

One full scan cycle: fetch listings for every configured search query,
filter out already-seen ones, run them through the filter logic (and AI
for genuinely ambiguous cases), calculate distance for matches, and send
Telegram notifications for anything new and relevant.

Run this via cron every N hours - it's a single run-and-exit script, not a
long-running process. Example crontab entry for every 3 hours:

    0 */3 * * * cd /path/to/marktplaats_monitor && /usr/bin/python3 main.py >> run.log 2>&1
"""

import logging
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

import storage
import scraper
import filters
import ai_classifier
import distance
import telegram_notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_scan_cycle(config: dict) -> None:
    storage.init_db()

    total_fetched = 0
    total_new = 0
    matches = []  # collected here, sent (sorted by distance) at the end

    for query in config["search_queries"]:
        try:
            listings = scraper.fetch_listings(
                query, config["base_search_url"], config["user_agent"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch listings for query '%s': %s", query, exc)
            continue

        total_fetched += len(listings)

        for listing in listings:
            if storage.is_seen(listing.listing_id):
                continue  # already processed in a previous run

            total_new += 1
            result = filters.evaluate_listing(listing.title, listing.description_snippet, config)

            accepted = result.accepted
            reason = result.reason

            # Ambiguous case ("mankement" without clear negation) - ask the AI
            if result.needs_ai_review:
                verdict = ai_classifier.classify_ambiguous_listing(
                    listing.combined_text, config["ai_model"]
                )
                accepted = verdict.relevant
                reason = f"AI review: {verdict.reason}"

            storage.mark_seen(listing.listing_id, listing.title, listing.url, accepted)

            if not accepted:
                logger.debug("Rejected '%s': %s", listing.title, reason)
                continue

            logger.info("MATCH: '%s' - %s", listing.title, reason)

            # The search-result date can be a bump/repost date, not the
            # true original posting date - fetch the real one from the
            # listing page itself (cheap here, since it's only done for
            # actual matches, not all scanned listings).
            accurate_date = scraper.get_accurate_posting_date(listing.url, config["user_agent"])
            if accurate_date:
                listing.posted_date = f"Sinds {accurate_date}"

            if listing.latitude is not None and listing.longitude is not None:
                dist_result = distance.get_driving_distance_from_coords(
                    listing.latitude, listing.longitude,
                    config["home_lat"], config["home_lon"],
                )
            else:
                # Fallback for the rare listing missing exact coordinates
                dist_result = distance.get_driving_distance(
                    listing.location_text or "Netherlands", config["home_location"]
                )

            matches.append(
                {
                    "listing": listing,
                    "reason": reason,
                    "distance_km": dist_result.distance_km,
                    "duration_minutes": dist_result.duration_minutes,
                }
            )

        time.sleep(config["request_delay_seconds"])

    # Sort FARTHEST-first, so they're sent first (appearing higher up in the
    # chat) and the CLOSEST listing is sent last, landing at the bottom where
    # Telegram opens by default - no scrolling needed to see the best option.
    # Listings with unknown distance (None) are sent first/topmost, since
    # they're the least useful to see immediately.
    matches.sort(
        key=lambda m: m["distance_km"] if m["distance_km"] is not None else float("inf"),
        reverse=True,
    )

    for match in matches:
        message = telegram_notifier.format_listing_message(
            title=match["listing"].title,
            price_text=match["listing"].price_text,
            url=match["listing"].url,
            match_reason=match["reason"],
            distance_km=match["distance_km"],
            duration_minutes=match["duration_minutes"],
            posted_date=match["listing"].posted_date,
        )
        telegram_notifier.send_listing(match["listing"].image_url, message)

    logger.info(
        "Scan complete. Fetched: %d | New: %d | Matched: %d | Total tracked: %d",
        total_fetched,
        total_new,
        len(matches),
        storage.count_seen(),
    )


if __name__ == "__main__":
    load_dotenv()  # loads .env into environment variables
    cfg = load_config()
    run_scan_cycle(cfg)
