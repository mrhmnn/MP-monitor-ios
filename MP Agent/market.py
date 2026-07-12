"""
market.py

Realistic market-price tracking for iPhones on Marktplaats - based on what
listings ACTUALLY go for (bids + listings that disappear = sale proxy),
not the asking prices of stale listings nobody buys.

Data model (tables created in storage.init_db, same seen_listings.db file
so the data-branch snapshot carries everything):

  market_listings - one row per listing with a parseable iPhone model,
                    both damaged (buy side) and working (resale side).
  price_obs       - append-only price/bid observations over time.

Three signals, in increasing order of realism:
  1. Asking prices  - available immediately, but optimistic.
  2. Bids           - real money offered; scraped from the listing page's
                      __NEXT_DATA__ "bids" array (scraper.fetch_listing_status).
  3. Closed prices  - a listing that disappears within days WITH bids on it
                      very likely sold; its last ask / highest bid is the
                      best sale-price proxy Marktplaats offers (there is no
                      public sold-price data). Listings that sat for weeks
                      and vanished probably just expired - reported
                      separately, never mixed into "sold" stats.

Everything here is best-effort and must never kill a scan: all entry
points called from main.py catch and log their own exceptions.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median, quantiles
from typing import Optional

import models
import scraper
import storage

logger = logging.getLogger(__name__)

# A closed listing counts as a likely SALE (not expiry) when it had at
# least one bid and was gone within this many days of first sighting.
SALE_MAX_DAYS = 7

_STORAGE_RE = re.compile(r"(\d+)\s*(GB|TB)", re.IGNORECASE)

# Damage vocabulary for the is_damaged split. The config primary_keywords
# are checked too (ingest gets config), but these broad stems catch damaged
# listings from the resale-side queries whose phrasing the precise keyword
# list misses - better to wrongly call a working phone "damaged" than to
# pollute the resale benchmark with broken ones.
_DAMAGE_STEMS = (
    "kapot", "defect", "schade", "beschadig", "gebarsten", "barst",
    "gebroken", "stuk", "voor onderdelen", "voor reparatie", "krasje",
    "krassen", "gebutst", "deuk", "werkt niet", "doet het niet",
    "laadt niet", "mankement", "gebrek",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_storage_gb(storage_text: str, title: str = "") -> Optional[int]:
    """'128 GB' -> 128; '1 TB' -> 1024; falls back to the title text."""
    for text in (storage_text, title):
        m = _STORAGE_RE.search(text or "")
        if m:
            value = int(m.group(1))
            return value * 1024 if m.group(2).upper() == "TB" else value
    return None


def _is_damaged(listing, config: dict) -> bool:
    text = listing.combined_text
    if any(stem in text for stem in _DAMAGE_STEMS):
        return True
    return any(kw in text for kw in config.get("primary_keywords", []))


# --- Ingest -----------------------------------------------------------------

def ingest_listings(listings, config: dict, db_path: Path = storage.DB_PATH) -> None:
    """Upsert every listing with a parseable model; record a price
    observation when the ask price changed. Called for every scanned query
    (damage queries AND the market_queries resale sweep). Never raises."""
    try:
        _ingest(listings, config, db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Market ingest failed: %s", exc)


def _ingest(listings, config: dict, db_path: Path) -> None:
    now = _now()
    with sqlite3.connect(db_path) as conn:
        for listing in listings:
            model = models.parse_model(listing.title)
            if not model or not listing.listing_id:
                continue
            row = conn.execute(
                "SELECT final_ask_cents, status FROM market_listings WHERE listing_id = ?",
                (listing.listing_id,),
            ).fetchone()
            ask = listing.price_cents or None
            if row is None:
                conn.execute(
                    """
                    INSERT INTO market_listings
                        (listing_id, model, storage_gb, condition, is_damaged,
                         price_type, title, url, first_seen_utc, last_seen_utc,
                         status, final_ask_cents)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        listing.listing_id, model,
                        parse_storage_gb(listing.storage_text, listing.title),
                        listing.condition, int(_is_damaged(listing, config)),
                        listing.price_type, listing.title, listing.url,
                        now, now, ask,
                    ),
                )
                if ask:
                    conn.execute(
                        "INSERT INTO price_obs (listing_id, ts_utc, ask_cents) VALUES (?, ?, ?)",
                        (listing.listing_id, now, ask),
                    )
            else:
                prev_ask, status = row
                # A listing we thought was gone showing up again = relist/bump.
                conn.execute(
                    """
                    UPDATE market_listings
                    SET last_seen_utc = ?, status = 'open', closed_utc = NULL,
                        final_ask_cents = COALESCE(?, final_ask_cents)
                    WHERE listing_id = ?
                    """,
                    (now, ask, listing.listing_id),
                )
                if ask and ask != prev_ask:
                    conn.execute(
                        "INSERT INTO price_obs (listing_id, ts_utc, ask_cents) VALUES (?, ?, ?)",
                        (listing.listing_id, now, ask),
                    )
        conn.commit()


# --- Bid polling ------------------------------------------------------------

def poll_bids(config: dict, db_path: Path = storage.DB_PATH) -> None:
    """Fetch detail pages for open bidding-type listings (oldest-checked
    first, capped per run) and record their current bids. Never raises."""
    try:
        _poll_bids(config, db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Market bid polling failed: %s", exc)


def _poll_bids(config: dict, db_path: Path) -> None:
    cap = config.get("market_bid_poll_per_run", 20)
    user_agent = config["user_agent"]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT listing_id, url, final_bid_cents, bid_count FROM market_listings
            WHERE status = 'open' AND price_type IN ('MIN_BID', 'FAST_BID')
            ORDER BY last_bid_check_utc IS NOT NULL, last_bid_check_utc ASC
            LIMIT ?
            """,
            (cap,),
        ).fetchall()

    checked = 0
    for listing_id, url, prev_bid, prev_count in rows:
        try:
            status = scraper.fetch_listing_status(url, user_agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bid check failed for %s: %s", listing_id, exc)
            continue
        checked += 1
        now = _now()
        with sqlite3.connect(db_path) as conn:
            if status.gone:
                _close_listing(conn, listing_id, now)
            elif status.bid_cents is not None:
                highest = status.bid_cents[0] if status.bid_cents else None
                conn.execute(
                    """
                    UPDATE market_listings
                    SET last_bid_check_utc = ?,
                        final_bid_cents = NULLIF(MAX(COALESCE(final_bid_cents, 0), COALESCE(?, 0)), 0),
                        bid_count = MAX(bid_count, ?)
                    WHERE listing_id = ?
                    """,
                    (now, highest, len(status.bid_cents), listing_id),
                )
                if highest and (highest != prev_bid or len(status.bid_cents) != prev_count):
                    conn.execute(
                        """
                        INSERT INTO price_obs (listing_id, ts_utc, highest_bid_cents, bid_count)
                        VALUES (?, ?, ?, ?)
                        """,
                        (listing_id, now, highest, len(status.bid_cents)),
                    )
            else:
                # Page live but bids unparseable - just move it to the back
                # of the queue.
                conn.execute(
                    "UPDATE market_listings SET last_bid_check_utc = ? WHERE listing_id = ?",
                    (now, listing_id),
                )
            conn.commit()
        time.sleep(0.5)
    if checked:
        logger.info("Market: bid-checked %d listings", checked)


# --- Closure detection ------------------------------------------------------

def _close_listing(conn: sqlite3.Connection, listing_id: str, now: str) -> None:
    conn.execute(
        """
        UPDATE market_listings
        SET status = 'gone', closed_utc = ?
        WHERE listing_id = ? AND status = 'open'
        """,
        (now, listing_id),
    )


def check_closures(config: dict, db_path: Path = storage.DB_PATH) -> None:
    """Listings unseen for market_stale_hours get one detail-page check:
    gone -> closed (sale proxy), still live -> last_seen bumped so it isn't
    rechecked immediately. Capped per run. Never raises."""
    try:
        _check_closures(config, db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Market closure check failed: %s", exc)


def _check_closures(config: dict, db_path: Path) -> None:
    cap = config.get("market_closure_checks_per_run", 10)
    stale_hours = config.get("market_stale_hours", 48)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()
    user_agent = config["user_agent"]

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT listing_id, url FROM market_listings
            WHERE status = 'open' AND last_seen_utc < ?
            ORDER BY last_seen_utc ASC
            LIMIT ?
            """,
            (cutoff, cap),
        ).fetchall()

    closed = 0
    for listing_id, url in rows:
        try:
            status = scraper.fetch_listing_status(url, user_agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Closure check failed for %s: %s", listing_id, exc)
            continue
        now = _now()
        with sqlite3.connect(db_path) as conn:
            if status.gone:
                _close_listing(conn, listing_id, now)
                closed += 1
            else:
                # Still live, just fell out of the newest-30 window. Bump
                # last_seen so it goes to the back of the recheck queue,
                # and take the free bid reading while we're here.
                highest = status.bid_cents[0] if status.bid_cents else None
                conn.execute(
                    """
                    UPDATE market_listings
                    SET last_seen_utc = ?,
                        final_bid_cents = NULLIF(MAX(COALESCE(final_bid_cents, 0), COALESCE(?, 0)), 0),
                        bid_count = MAX(bid_count, COALESCE(?, 0))
                    WHERE listing_id = ?
                    """,
                    (now, highest, len(status.bid_cents or []), listing_id),
                )
            conn.commit()
        time.sleep(0.5)
    if rows:
        logger.info("Market: closure-checked %d listings, %d gone", len(rows), closed)


# --- Benchmarks -------------------------------------------------------------

def benchmark(
    model: str,
    storage_gb: Optional[int] = None,
    damaged: Optional[bool] = None,
    window_days: int = 30,
    db_path: Path = storage.DB_PATH,
) -> dict:
    """Return price stats (euros) for one model segment:
    {
      "n_open", "ask_median", "ask_p25", "ask_p75",
      "n_bids", "bid_median",
      "n_sold", "sold_median",     # gone within SALE_MAX_DAYS with >=1 bid
      "n_expired",                 # gone without bids / after sitting long
    }
    Missing stats are None. `damaged=None` means both sides combined.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    where = "model = ? AND last_seen_utc >= ?"
    params: list = [model, cutoff]
    if storage_gb is not None:
        where += " AND storage_gb = ?"
        params.append(storage_gb)
    if damaged is not None:
        where += " AND is_damaged = ?"
        params.append(int(damaged))

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT status, final_ask_cents, final_bid_cents, bid_count,
                   first_seen_utc, closed_utc
            FROM market_listings WHERE {where}
            """,
            params,
        ).fetchall()

    asks, bids, sold, expired = [], [], [], 0
    for status_, ask, bid, bid_count, first_seen, closed_utc in rows:
        if status_ == "open":
            if ask:
                asks.append(ask / 100)
            if bid:
                bids.append(bid / 100)
        else:
            days_listed = None
            if closed_utc and first_seen:
                days_listed = (
                    datetime.fromisoformat(closed_utc) - datetime.fromisoformat(first_seen)
                ).total_seconds() / 86400
            if bid_count and days_listed is not None and days_listed <= SALE_MAX_DAYS:
                # Likely a real sale: best proxy = highest of last ask / top bid
                sold.append(max(ask or 0, bid or 0) / 100)
            else:
                expired += 1

    def _stats(values):
        if not values:
            return None, None, None
        if len(values) < 4:
            return median(values), None, None
        q = quantiles(values, n=4)
        return median(values), q[0], q[2]

    ask_median, ask_p25, ask_p75 = _stats(asks)
    return {
        "n_open": len(asks),
        "ask_median": ask_median, "ask_p25": ask_p25, "ask_p75": ask_p75,
        "n_bids": len(bids),
        "bid_median": median(bids) if bids else None,
        "n_sold": len(sold),
        "sold_median": median(sold) if sold else None,
        "n_expired": expired,
    }


def benchmark_line(model: Optional[str], config: dict) -> str:
    """One [MARKT] alert line for a damaged-listing match, e.g.:

        📊 [MARKT] iphone 15 schade: vraag mediaan €150 (n=12) · sold ~€135 (n=6)

    Returns "" when there's no model or not enough data yet (n below
    market_bench_min_n) - alerts stay noise-free until stats mean something.
    Never raises."""
    try:
        if not model:
            return ""
        min_n = config.get("market_bench_min_n", 5)
        stats = benchmark(
            model, damaged=True,
            window_days=config.get("market_bench_window_days", 30),
        )
        parts = []
        if stats["ask_median"] is not None and stats["n_open"] >= min_n:
            parts.append(f"vraag mediaan €{stats['ask_median']:.0f} (n={stats['n_open']})")
        if stats["bid_median"] is not None and stats["n_bids"] >= 3:
            parts.append(f"bod mediaan €{stats['bid_median']:.0f} (n={stats['n_bids']})")
        if stats["sold_median"] is not None and stats["n_sold"] >= 3:
            parts.append(f"verkocht ~€{stats['sold_median']:.0f} (n={stats['n_sold']})")
        if not parts:
            return ""
        return f"📊 [MARKT] {model} schade: " + " · ".join(parts)
    except Exception as exc:  # noqa: BLE001
        logger.error("benchmark_line failed for %s: %s", model, exc)
        return ""
