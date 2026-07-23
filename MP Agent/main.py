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


def _send_bargains(listings, config: dict) -> int:
    """Alert on working phones priced well under their model's market value.

    A second, independent deal source alongside the damage pipeline (added
    2026-07-23). A working iPhone 15 at EUR 250 against a EUR 405 market is
    a better flip than most damaged ones - no parts, no wait, no risk the
    damage turns out to be a board fault.

    Reuses the resale-sweep listings already in memory, so it costs no
    extra search requests. Only the handful that survive scoring cost a
    detail-page fetch. Returns the number of alerts sent; never raises.
    """
    sent = 0
    try:
        candidates = market.find_bargains(listings, config)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bargain sweep failed: %s", exc)
        return 0

    for listing, score in candidates:
        # Reuse the normal noise gates. A bargain that's actually a shop
        # listing, a wanted ad or a wholesale lot is still junk - and the
        # price-anomaly angle makes those MORE likely to surface, since
        # that's exactly the shape of a too-good-to-be-true listing.
        verdict = filters.evaluate_listing(
            listing.title, listing.description_snippet, config,
            seller_has_website=listing.seller_has_website,
            priority_product=listing.priority_product,
        )
        if verdict.reason.startswith((
            "seller has a business website", "paid promoted listing",
            "bulk lot", "looks like a business", "looks like a 'wanted",
            "title mentions LCD",
        )):
            logger.info(
                "Bargain skipped '%s': %s", listing.title, verdict.reason
            )
            market.mark_bargain_alerted(listing.listing_id)
            continue

        if listing.latitude is not None and listing.longitude is not None:
            dist = distance.get_driving_distance_from_coords(
                listing.latitude, listing.longitude,
                config["home_lat"], config["home_lon"],
            )
        else:
            dist = distance.get_driving_distance(
                listing.location_text or "Netherlands", config["home_location"]
            )
        max_km = config.get("max_distance_km")
        if max_km is not None and dist.distance_km is not None and dist.distance_km > max_km:
            # Mark it so the same out-of-range listing isn't re-scored and
            # re-geocoded on every one of the ~180 runs a day.
            market.mark_bargain_alerted(listing.listing_id)
            continue

        details = scraper.fetch_listing_details(listing.url, config["user_agent"])
        message = telegram_notifier.format_listing_message(
            title=listing.title,
            price_text=listing.price_text,
            url=listing.url,
            match_reason="Werkend toestel onder marktprijs (koopje-sweep)",
            distance_km=dist.distance_km,
            duration_minutes=dist.duration_minutes,
            posted_date=details.posted_date or listing.posted_date,
            city=listing.location_text,
            market_line=market.bargain_line(score, listing.price_cents),
            is_reserved=details.is_reserved,
            posted_iso=details.posted_iso,
        )
        # Marked BEFORE sending: a send that fails is logged loudly below,
        # but re-alerting the same listing every 8 minutes forever would be
        # far worse than losing one notification.
        market.mark_bargain_alerted(listing.listing_id)
        if telegram_notifier.send_listing(listing.image_url, message):
            sent += 1
            logger.info(
                "BARGAIN: '%s' - %.0f%% under market - %s",
                listing.title, score["headroom_pct"] * 100, listing.url,
            )
        else:
            logger.error(
                "ALERT LOST: bargain send failed for '%s' - %s",
                listing.title, listing.url,
            )
    return sent


def run_scan_cycle(config: dict) -> None:
    storage.init_db()

    total_fetched = 0
    total_new = 0
    failed_queries = 0
    ai_calls = 0
    skipped_too_far = 0
    sent_matches = 0
    failed_sends = 0

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
            model_key = models.parse_model(listing.title)
            market_line = market.benchmark_line(model_key, config)
            # 💸 verdict line: this listing's asking price scored against
            # that resale value. Turns 30 identical-looking alerts a day
            # into a ranked list at a glance.
            deal = market.deal_line(
                model_key, listing.price_cents, listing.price_type, config
            )

            message = telegram_notifier.format_listing_message(
                title=listing.title,
                price_text=listing.price_text,
                url=listing.url,
                match_reason=reason,
                distance_km=dist_result.distance_km,
                duration_minutes=dist_result.duration_minutes,
                posted_date=listing.posted_date,
                city=listing.location_text,
                market_line=market_line,
                is_reserved=details.is_reserved if details else False,
                deal_line=deal,
                posted_iso=details.posted_iso if details else "",
            )
            # Sent IMMEDIATELY, not batched to the end of the scan
            # (changed 2026-07-23). Alerts used to be collected across all
            # ~35 queries and dispatched after the market bid-polling and
            # closure-check passes, which put 3-5 minutes between finding a
            # listing and Milad's phone buzzing - on a scan that runs every
            # ~8 minutes, and in a market where damaged phones now sell
            # 2.4x faster than two weeks ago. The batching existed only to
            # sort alerts by distance, but at ~30 alerts across ~180 runs a
            # day, over 90% of runs produce zero or one match, so the sort
            # was almost never doing anything - it was pure latency.
            if telegram_notifier.send_listing(listing.image_url, message):
                sent_matches += 1
            else:
                # The listing is already marked seen, so a failed send is a
                # permanently lost alert - make it impossible to miss.
                failed_sends += 1
                logger.error(
                    "ALERT LOST: Telegram send failed for '%s' - %s",
                    listing.title, listing.url,
                )

        time.sleep(config["request_delay_seconds"])

    # --- Market-price tracking (resale side + bids + closures) ---
    # Resale sweep: broad per-generation queries whose listings only feed
    # the price tracker (working phones = what a repaired flip sells for).
    # They deliberately do NOT go through the match/alert pipeline.
    bargains_sent = 0
    for query in config.get("market_queries", []):
        try:
            listings = scraper.fetch_listings(
                query, config["base_search_url"], config["user_agent"]
            )
            market.ingest_listings(listings, config)
            # Bargain sweep: these are WORKING phones. Any priced well
            # under their model's market value is a flip with no repair
            # cost at all - previously they fed statistics and were
            # discarded. Zero extra requests: same listings, already
            # fetched and parsed. See market.find_bargains for the guards.
            bargains_sent += _send_bargains(listings, config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Market query '%s' failed: %s", query, exc)
        time.sleep(config["request_delay_seconds"])

    # Bid readings for open auction listings + sold/removed detection for
    # listings that dropped off the radar. Both capped per run and
    # best-effort - a pricing hiccup must never break the scan.
    market.poll_bids(config)
    market.check_closures(config)

    logger.info(
        "Scan complete. Fetched: %d | New: %d | Matched: %d | Bargains: %d | "
        "Too far: %d | AI calls: %d | Failed sends: %d | Total tracked: %d",
        total_fetched,
        total_new,
        sent_matches,
        bargains_sent,
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
    # PARTIAL breakage is the realistic failure mode, and the absolute
    # threshold above could never see it (fixed 2026-07-23). With ~35
    # queries returning up to 30 listings each, a healthy run fetches
    # 800-1100 - so "fewer than 30 total" only fires when essentially
    # everything is dead. Thirty of thirty-five queries could fail and
    # still clear it comfortably, silently gutting coverage for days.
    # That is the exact class of silent degradation this check exists for.
    failure_ratio = failed_queries / total_queries if total_queries else 0
    max_failure_ratio = config.get("alert_max_query_failure_ratio", 0.25)
    too_many_failed = failure_ratio > max_failure_ratio
    all_queries_failed = total_queries > 0 and failed_queries == total_queries

    if total_fetched < min_expected or all_queries_failed or too_many_failed:
        alert_lines = [
            "⚠️ <b>Scan health warning</b>",
            f"Only {total_fetched} listings fetched across {total_queries} queries "
            f"(expected at least {min_expected}).",
        ]
        if failed_queries:
            alert_lines.append(
                f"{failed_queries} of {total_queries} queries raised an error "
                f"({failure_ratio * 100:.0f}%)."
            )
        alert_lines.append(
            "This usually means Marktplaats changed something or is blocking "
            "requests, not that there are genuinely fewer listings today. Worth "
            "checking the scraper manually."
        )
        alert_sent = telegram_notifier.send_message("\n".join(alert_lines))
        logger.warning(
            "Scan health warning (fetched %d, %d/%d queries failed) - alert %s",
            total_fetched, failed_queries, total_queries,
            "sent" if alert_sent else "FAILED TO SEND",
        )


if __name__ == "__main__":
    load_dotenv()  # loads .env into environment variables
    cfg = load_config()
    run_scan_cycle(cfg)
