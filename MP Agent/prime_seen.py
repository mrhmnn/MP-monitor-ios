"""
prime_seen.py

Quiet restart: absorb the current backlog into `seen_listings` WITHOUT
classifying it and WITHOUT notifying anything.

WHY THIS EXISTS (2026-11-23)
----------------------------
When the AI half of the pipeline is down, main.py deliberately does NOT
mark an AI-review listing as seen:

    logger.warning("AI call failed for '%s' - leaving unseen to retry next run")

That is the right call for a blip - a listing worth EUR 300 of margin
should not be thrown away because one API call failed. But across a long
outage it builds a standing pool of unseen-but-still-live listings (~300
on 2026-11-23, with the Anthropic credit balance empty and an AI failure
streak of 3624). The moment the AI works again, that whole pool gets
classified in a single run and every accept fires at once - a burst of
alerts for listings that are by then hours or days old and already picked
over by everyone else.

This script defuses that: it walks the same search queries a scan walks,
and for every listing that has no row yet it writes one marked
`matched = 0`, `outcome = 'primed'`. Consequences, all deliberate:

  - No AI call, so it costs nothing and works with the API down.
  - No Telegram send, ever. This module does not import the notifier.
  - `matched = 0` means the reappear path can never resurrect these
    either: storage.check_reappeared() is only consulted when
    seen_record["matched"] is true (main.py), so a primed listing stays
    silent even when it bounces back into the newest-30 window.

THE COST, STATED PLAINLY
------------------------
A primed listing is silenced permanently. If a genuinely good deal is
live right now, priming buries it along with the junk. That is the exact
trade the standing rule in the vault warns against ("recall over a quiet
phone"), so it is NOT the default posture of this project - it is a
one-off tool for restarting after an outage, and `--older-than-hours`
exists to keep the blast radius off today's listings:

    python prime_seen.py --older-than-hours 24   # bury the stale pile,
                                                 # leave today alertable

With a cutoff set, a listing whose post date cannot be parsed is treated
as FRESH and left alone. Failing toward "still alertable" is the only
tolerable direction for a tool whose other outcome is permanent silence.

USAGE
-----
    python prime_seen.py --dry-run                 # count, change nothing
    python prime_seen.py                           # bury everything visible
    python prime_seen.py --older-than-hours 24     # bury only the stale pile

Run it BEFORE restoring API credits / the dispatcher cadence, not after:
the point is to be holding the seen-rows before the first healthy scan.
The prime.yml workflow does this on the runner so the DB on the `data`
branch is the one that gets updated.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import yaml

import market
import scraper
import storage

logger = logging.getLogger("prime_seen")


def parse_posted(posted_date: str):
    """Best-effort parse of the LRP API's `date` field to an aware datetime.

    Returns None when it cannot be read, which callers must treat as
    "assume fresh" - see the module docstring.
    """
    if not posted_date:
        return None
    text = posted_date.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_stale_enough(listing, cutoff) -> bool:
    """True when this listing may be primed.

    `cutoff` is None for "no age filter - prime everything visible".
    """
    if cutoff is None:
        return True
    posted = parse_posted(getattr(listing, "posted_date", ""))
    if posted is None:
        return False  # unparseable date -> leave it alertable
    return posted < cutoff


def prime(
    config: dict,
    older_than_hours: float = 0.0,
    dry_run: bool = False,
    db_path=None,
) -> dict:
    """Walk every search query and bury the listings that have no row yet.

    Returns a counters dict; callers log it rather than this function
    printing, so the tests can assert on numbers instead of stdout.

    `db_path` resolves at CALL time and is then passed explicitly into every
    storage/market call. The older functions in storage.py carry a
    `db_path: Path = DB_PATH` default that binds at IMPORT, so a test that
    monkeypatches storage.DB_PATH does not redirect them - it writes to the
    production database instead (storage.get_health_value's docstring
    records the same trap). This script's whole job is writing permanent
    silence into that file, so it never relies on a default.
    """
    db_path = db_path or storage.DB_PATH
    storage.init_db(db_path)

    cutoff = None
    if older_than_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)

    stats = {
        "queries": 0,
        "failed_queries": 0,
        "fetched": 0,
        "already_seen": 0,
        "primed": 0,
        "left_fresh": 0,
    }
    reason = (
        f"primed {datetime.now(timezone.utc).date().isoformat()}: quiet restart, "
        "backlog suppressed without classification"
    )

    for query in config["search_queries"]:
        stats["queries"] += 1
        try:
            listings = scraper.fetch_listings(
                query, config["base_search_url"], config["user_agent"]
            )
        except Exception as exc:  # noqa: BLE001
            # Same posture as run_scan_cycle: a failed query is logged and
            # skipped. It matters more here, though - a query that 403s
            # leaves its listings unprimed, so they can still alert on the
            # first healthy scan. The summary reports the count so a mostly
            # blocked run is visible rather than silently partial.
            logger.error("Failed to fetch listings for query '%s': %s", query, exc)
            stats["failed_queries"] += 1
            continue

        stats["fetched"] += len(listings)

        # Keep feeding the price tracker. The market tables are what
        # pricebench.py and the weekly summary read, and nothing in them
        # notifies on its own - ingest is pure data. Skipping it would
        # leave a hole in the price history for no benefit.
        if not dry_run:
            market.ingest_listings(listings, config, db_path=db_path)

        for listing in listings:
            if storage.get_seen_record(listing.listing_id, db_path=db_path) is not None:
                stats["already_seen"] += 1
                continue
            if not is_stale_enough(listing, cutoff):
                stats["left_fresh"] += 1
                continue
            stats["primed"] += 1
            if dry_run:
                continue
            storage.mark_seen(
                listing.listing_id,
                listing.title,
                listing.url,
                False,  # matched = 0: also blocks the reappear re-alert path
                reason,
                db_path=db_path,
            )
            # 'primed' rather than the 'rejected' mark_seen writes for a
            # non-match, so /why-no-alert and the probe review can tell a
            # deliberate burial apart from a filter or AI rejection.
            storage.set_outcome(listing.listing_id, "primed", db_path=db_path)
            # The koopje sweep dedups in market_listings, not seen_listings
            # (see market.mark_bargain_alerted), so burying a listing here
            # does NOT stop it firing a bargain alert. Mark it there too,
            # or the quiet restart leaks alerts through the other channel.
            market.mark_bargain_alerted(listing.listing_id, db_path=db_path)

    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Absorb the current listing backlog without alerting on it."
    )
    parser.add_argument(
        "--older-than-hours",
        type=float,
        default=0.0,
        help=(
            "Only bury listings posted more than this many hours ago. "
            "0 (default) buries everything currently visible."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be buried and write nothing.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    with open("config.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    stats = prime(
        config,
        older_than_hours=args.older_than_hours,
        dry_run=args.dry_run,
    )

    scope = (
        "everything currently visible"
        if args.older_than_hours <= 0
        else f"listings older than {args.older_than_hours:g}h"
    )
    logger.info(
        "%sPrimed %d listing(s) [%s] | already seen: %d | left alertable: %d | "
        "fetched: %d across %d queries (%d failed) | total tracked: %d",
        "DRY RUN - nothing written. " if args.dry_run else "",
        stats["primed"],
        scope,
        stats["already_seen"],
        stats["left_fresh"],
        stats["fetched"],
        stats["queries"],
        stats["failed_queries"],
        storage.count_seen(),
    )
    if stats["failed_queries"]:
        logger.warning(
            "%d query/queries failed - their listings were NOT primed and can "
            "still alert on the next healthy scan. Re-run if that matters.",
            stats["failed_queries"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
