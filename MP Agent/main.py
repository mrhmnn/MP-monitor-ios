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
    failed_queries = 0
    matches = []  # collected here, sent (sorted by distance) at the end

    for query in config["search_queries"]:
        try:
            listings = scraper.fetch_listings(
                query, config["base_search_url"], config["user_agent"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch listings for query '%s': %s", query, exc)
            failed_queries += 1
            continue

        total_fetched += len(listings)

        for listing in listings:
            seen_record = storage.get_seen_record(listing.listing_id)

            if seen_record is None:
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
            else:
                # Already processed before. Each scan only pulls the newest-30
                # results per query, so a listing that's been off our radar
                # for a while and just resurfaced was almost certainly
                # relisted/bumped - re-notify on it if it was a match before.
                # Otherwise it's just the same listing still sitting in the
                # newest-30 window, or a previously-rejected one - nothing new.
                reappeared = storage.check_reappeared(
                    listing.listing_id, config.get("reappear_gap_hours", 24)
                )
                storage.touch_last_seen(listing.listing_id)

                if not (reappeared and seen_record["matched"]):
                    continue

                accepted = True
                reason = "Reappeared after being off-market - originally matched"

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

    # Health check: a suspiciously low fetch count almost always means
    # something broke (site structure changed, requests started getting
    # blocked, etc.) rather than there genuinely being few listings today -
    # 22 queries returning next to nothing is not a normal day. This is the
    # exact failure mode that went unnoticed for hours the first time
    # around, so it gets flagged loudly instead of failing silently.
    total_queries = len(config["search_queries"])
    min_expected = config.get("alert_min_total_fetched", 30)
    all_queries_failed = total_queries > 0 and failed_queries == total_queries

    if total_fetched < min_expected or all_queries_failed:
        alert_lines = [
            "⚠️ *Scan health warning*",
            f"Only {total_fetched} listings fetched across {total_queries} queries "
            f"(expected at least {min_expected}).",
        ]
        if failed_queries:
            alert_lines.append(f"{failed_queries} of {total_queries} queries raised an error.")
        alert_lines.append(
            "This usually means Marktplaats changed something or is blocking "
            "requests, not that there are genuinely fewer listings today. Worth "
            "checking the scraper manually."
        )
        alert_sent = telegram_notifier.send_message("\n".join(alert_lines))
        logger.warning(
            "Low fetch count detected (%d < %d) - health alert %s",
            total_fetched, min_expected,
            "sent" if alert_sent else "FAILED TO SEND",
        )


if __name__ == "__main__":
    load_dotenv()  # loads .env into environment variables
    cfg = load_config()
    run_scan_cycle(cfg)
