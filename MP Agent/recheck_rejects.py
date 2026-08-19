"""Re-run the CURRENT classifier over listings the OLD one rejected.

The scraper never looks at a listing twice. That is the right default for a
scanner - but it means every prompt fix silently strands the listings the old
prompt got wrong. They sit in `seen_listings` with matched=0, age out of the
newest-30 fetch window within a few days, and are never reconsidered.

That is not hypothetical. Two prompt fixes in August 2026 each left a backlog:

  30b5af5 (08-09)  unspecified "schade" is missing info, not expensive damage
  901f9c2 (08-19)  "voor onderdelen" framing is not evidence of a board fault

After 901f9c2 shipped, five still-live phones were sitting in the DB rejected
on reasoning the current prompt no longer endorses - including "Gebroken
iPhone 15 - Perfect voor onderdelen" (killed as "likely unsuitable for resale
or contains expensive damage") and "iPhone 17 - Lichte schade". Milad found
them by hand. This script is so nobody has to do that again.

/requeue-listing does NOT cover this: deleting a seen-row only helps if the
listing is still inside some query's newest-30, and anything more than a day
or two old no longer is. This re-classifies directly instead.

Usage (from `MP Agent/`):
    python recheck_rejects.py --days 14              # dry run, prints verdicts
    python recheck_rejects.py --days 14 --telegram   # also alert on flips
    python recheck_rejects.py --db /path/to/seen.db  # explicit DB

Run it after every prompt or filter change that LOOSENS a rejection.

Deliberately does not write to the database. Every scan force-pushes the data
branch, so a concurrent write here would either be clobbered or clobber the
scan (the 2026-07-13 race). Alerting is the useful half; the row can stay
rejected, since a listing that flips is one Milad is about to act on anyway.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

import yaml

import ai_classifier
import models
import scraper
import telegram_notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("recheck")

# Rejections worth re-testing. A prompt fix that loosens things changes
# verdicts that turned on ABSENCE of evidence ("no specific defect named",
# "likely expensive") - not ones that named a real disqualifying fault
# ("iCloud lock", "water damage"). Filtering here keeps the Haiku spend
# proportional to the backlog that could actually have moved.
_SPECULATIVE_MARKERS = (
    "likely",
    "unclear",
    "suggest",
    "probably",
    "no specific defect",
    "cannot be classified",
    "not described",
    "no damage defect",
    "unspecified",
    "without specifying",
    "vague",
)


def _is_speculative(reason: str) -> bool:
    low = (reason or "").lower()
    return any(m in low for m in _SPECULATIVE_MARKERS)


def load_config() -> dict:
    with open(Path(__file__).parent / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def candidates(db_path: str, days: int) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT listing_id, title, url, reason
        FROM seen_listings
        WHERE matched = 0
          AND reason LIKE 'AI review:%'
          AND first_seen_utc > datetime('now', ?)
        ORDER BY first_seen_utc DESC
        """,
        (f"-{days} days",),
    ).fetchall()
    conn.close()
    return [r for r in rows if _is_speculative(r[3])]


def recheck(db_path: str, days: int, limit: int, send: bool) -> int:
    config = load_config()
    ua = config["user_agent"]
    high_value_models = set(config.get("high_value_models", []))

    rows = candidates(db_path, days)
    logger.info(
        "%d AI rejects in the last %d days turned on absent evidence - re-testing "
        "the %d most recent",
        len(rows), days, min(limit, len(rows)),
    )

    flipped = 0
    for listing_id, title, url, old_reason in rows[:limit]:
        status = scraper.fetch_listing_status(url, ua)
        if status.gone:
            logger.info("gone      %s  %s", listing_id, title[:58])
            continue

        details = scraper.fetch_listing_details(url, ua)
        ai_input = f"{title}\n{details.description}" if details.description else title
        high_value = models.parse_model(title) in high_value_models
        verdict = ai_classifier.classify_ambiguous_listing(
            ai_input,
            config["ai_model"],
            high_value=high_value,
            image_urls=getattr(details, "image_urls", None),
            max_images=config.get("ai_max_images", 3),
        )

        if not verdict.relevant:
            logger.info("still no  %s  %s", listing_id, title[:58])
            continue

        flipped += 1
        logger.info("FLIPPED   %s  %s", listing_id, title[:58])
        logger.info("    was: %s", old_reason[:150])
        logger.info("    now: %s", verdict.reason[:150])
        logger.info("    %s", url)

        if send:
            message = telegram_notifier.format_listing_message(
                title=title,
                price_text="",
                url=url,
                match_reason=f"RE-CHECK (prompt fix): {verdict.reason}",
                distance_km=None,
                duration_minutes=None,
                posted_date=details.posted_date,
                is_reserved=details.is_reserved,
                posted_iso=details.posted_iso,
            )
            if telegram_notifier.send_message(message):
                logger.info("    alert sent")
            else:
                logger.error("    ALERT LOST for %s", listing_id)

    logger.info("done - %d listing(s) the current prompt would now accept", flipped)
    return flipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="seen_listings.db", help="path to seen_listings.db")
    parser.add_argument("--days", type=int, default=14, help="how far back to look")
    parser.add_argument("--limit", type=int, default=40, help="cap on listings re-classified")
    parser.add_argument(
        "--telegram", action="store_true", help="send an alert for each flipped listing"
    )
    args = parser.parse_args()
    recheck(args.db, args.days, args.limit, args.telegram)


if __name__ == "__main__":
    main()
