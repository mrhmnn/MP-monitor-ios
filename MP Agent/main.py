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
import damage_detect
import distance
import market
import models
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
    ai_calls = 0
    skipped_too_far = 0
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

        # Feed everything into the market-price tracker (buy-side data:
        # these are all damage queries). Best-effort, never raises.
        market.ingest_listings(listings, config)

        for listing in listings:
            seen_record = storage.get_seen_record(listing.listing_id)
            details = None  # one listing-page fetch, shared by AI review + match handling

            if seen_record is None:
                total_new += 1
                result = filters.evaluate_listing(
                    listing.title,
                    listing.description_snippet,
                    config,
                    seller_has_website=listing.seller_has_website,
                    priority_product=listing.priority_product,
                )

                accepted = result.accepted
                reason = result.reason

                # Ambiguous case ("mankement" without clear negation) - ask the AI
                if result.needs_ai_review:
                    # The search-results description is truncated at ~200
                    # chars - the listing page's server-rendered description
                    # is a bit longer/cleaner (though Marktplaats truncates
                    # that too for anonymous requests, see
                    # scraper.fetch_listing_details). Only fetched for the
                    # few AI-review cases per run, not every listing.
                    details = scraper.fetch_listing_details(listing.url, config["user_agent"])
                    ai_input = (
                        f"{listing.title}\n{details.description}"
                        if details.description else listing.combined_text
                    )
                    ai_calls += 1
                    verdict = ai_classifier.classify_ambiguous_listing(
                        ai_input, config["ai_model"]
                    )
                    accepted = verdict.relevant
                    reason = f"AI review: {verdict.reason}"
                    if not accepted and verdict.reason.startswith("classification error"):
                        # API failure, not a judgment (e.g. 529 overloaded).
                        # Don't mark seen: the next run (~8 min) will retry.
                        # Found in the 07-18 probe review: three listings were
                        # permanently buried as rejects this way, one of them
                        # a textbook target (cracks front + back).
                        logger.warning(
                            "AI call failed for '%s' - leaving unseen to retry "
                            "next run", listing.title,
                        )
                        continue
                    logger.info(
                        "AI verdict for '%s': %s - %s",
                        listing.title, "RELEVANT" if accepted else "rejected", verdict.reason,
                    )
                elif not accepted and not reason.startswith("not a target model"):
                    # Recall probe (logging only, replaces the lost
                    # damage_filter.py work): the broad damage detector
                    # disagreeing with a no-AI rejection is exactly the
                    # bucket where silent misses hide. Reviewed weekly;
                    # promotes terms into config.yaml when patterns emerge.
                    # Model-mismatch rejections are excluded (2026-07-15):
                    # damage_detect has zero model awareness, so every
                    # off-target listing (12/13/SE/XR/...) mentioning any
                    # damage word fired a false disagreement - live-checked
                    # 83% of all probe firings were this, drowning the
                    # small number of real candidate misses.
                    damaged, terms = damage_detect.is_damaged(
                        listing.title, listing.description_snippet
                    )
                    if damaged:
                        logger.info(
                            "DISAGREEMENT probe: rejected without AI (%s) but "
                            "damage_detect sees %s in '%s' - %s",
                            reason, terms, listing.title, listing.url,
                        )

                storage.mark_seen(
                    listing.listing_id, listing.title, listing.url, accepted, reason
                )
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
                # INFO, not DEBUG: these lines are the first thing to grep
                # when a listing that should have alerted didn't. Bounded -
                # only new listings reach this point.
                logger.info("Rejected '%s': %s", listing.title, reason)
                continue

            logger.info("MATCH: '%s' - %s", listing.title, reason)

            # The search-result date can be a bump/repost date, not the true
            # original posting date - the listing page has the real one
            # (plus the reserved flag). Reuse the AI-review fetch if there
            # was one; only matches cost this extra request otherwise.
            if details is None:
                details = scraper.fetch_listing_details(listing.url, config["user_agent"])
            if details.posted_date:
                listing.posted_date = f"Sinds {details.posted_date}"

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

            # Distance cutoff: alerts are only useful if the phone is
            # actually collectable. Listings with UNKNOWN distance still
            # go through (better a manual look at the city name than a
            # silently dropped match), but a known distance beyond the
            # radius is a hard skip.
            max_km = config.get("max_distance_km")
            if (
                max_km is not None
                and dist_result.distance_km is not None
                and dist_result.distance_km > max_km
            ):
                skipped_too_far += 1
                logger.info(
                    "Skipping '%s' - %.0f km away (max %d km)",
                    listing.title, dist_result.distance_km, max_km,
                )
                continue

            # [MARKT] price context for the alert: what WERKEND phones of
            # this model actually go for, i.e. the resale side. Empty string
            # until enough data has accumulated - the alert omits the line.
            market_line = market.benchmark_line(
                models.parse_model(listing.title), config
            )

            matches.append(
                {
                    "listing": listing,
                    "reason": reason,
                    "market_line": market_line,
                    "distance_km": dist_result.distance_km,
                    "duration_minutes": dist_result.duration_minutes,
                    "is_reserved": details.is_reserved if details else False,
                }
            )

        time.sleep(config["request_delay_seconds"])

    # --- Market-price tracking (resale side + bids + closures) ---
    # Resale sweep: broad per-generation queries whose listings only feed
    # the price tracker (working phones = what a repaired flip sells for).
    # They deliberately do NOT go through the match/alert pipeline.
    for query in config.get("market_queries", []):
        try:
            listings = scraper.fetch_listings(
                query, config["base_search_url"], config["user_agent"]
            )
            market.ingest_listings(listings, config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Market query '%s' failed: %s", query, exc)
        time.sleep(config["request_delay_seconds"])

    # Bid readings for open auction listings + sold/removed detection for
    # listings that dropped off the radar. Both capped per run and
    # best-effort - a pricing hiccup must never break the scan.
    market.poll_bids(config)
    market.check_closures(config)

    # Sort FARTHEST-first, so they're sent first (appearing higher up in the
    # chat) and the CLOSEST listing is sent last, landing at the bottom where
    # Telegram opens by default - no scrolling needed to see the best option.
    # Listings with unknown distance (None) are sent first/topmost, since
    # they're the least useful to see immediately.
    matches.sort(
        key=lambda m: m["distance_km"] if m["distance_km"] is not None else float("inf"),
        reverse=True,
    )

    failed_sends = 0
    for match in matches:
        message = telegram_notifier.format_listing_message(
            title=match["listing"].title,
            price_text=match["listing"].price_text,
            url=match["listing"].url,
            match_reason=match["reason"],
            distance_km=match["distance_km"],
            duration_minutes=match["duration_minutes"],
            posted_date=match["listing"].posted_date,
            city=match["listing"].location_text,
            market_line=match["market_line"],
            is_reserved=match["is_reserved"],
        )
        if not telegram_notifier.send_listing(match["listing"].image_url, message):
            # The listing is already marked seen, so a failed send is a
            # permanently lost alert - make it impossible to miss in the log.
            failed_sends += 1
            logger.error(
                "ALERT LOST: Telegram send failed for '%s' - %s",
                match["listing"].title, match["listing"].url,
            )

    logger.info(
        "Scan complete. Fetched: %d | New: %d | Matched: %d | Too far: %d | "
        "AI calls: %d | Failed sends: %d | Total tracked: %d",
        total_fetched,
        total_new,
        len(matches),
        skipped_too_far,
        ai_calls,
        failed_sends,
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
            "⚠️ <b>Scan health warning</b>",
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
