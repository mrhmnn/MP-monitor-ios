"""
storage.py

Tracks which Marktplaats listings we've already processed, so we never
re-notify on the same listing twice. Uses SQLite - plenty for this scale,
no need for anything heavier (see project notes on why we skipped Postgres).
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "seen_listings.db"


def init_db(db_path: Path = DB_PATH) -> None:
    """Create the seen_listings and geocode_cache tables if they don't exist yet."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                listing_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                matched INTEGER NOT NULL,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL
            )
            """
        )
        # Migrate databases created before last_seen_utc existed - ALTER TABLE
        # can't express "IF NOT EXISTS" for a column in SQLite, so check first.
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(seen_listings)")}
        if "last_seen_utc" not in existing_columns:
            conn.execute("ALTER TABLE seen_listings ADD COLUMN last_seen_utc TEXT")
            conn.execute(
                "UPDATE seen_listings SET last_seen_utc = first_seen_utc WHERE last_seen_utc IS NULL"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS geocode_cache (
                place_name TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            )
            """
        )
        # --- Market-price tracking (market.py) ---
        # One row per tracked listing with a parseable iPhone model. Lives in
        # the same DB file so the data-branch snapshot in scan.yml carries it
        # without any workflow changes. Prices are cents (Marktplaats native).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_listings (
                listing_id TEXT PRIMARY KEY,
                model TEXT NOT NULL,               -- "iphone 15 pro max" (models.parse_model key)
                storage_gb INTEGER,                -- 128/256/... NULL if unknown
                condition TEXT,                    -- Marktplaats condition attribute
                is_damaged INTEGER NOT NULL,       -- 1 = damaged (buy side), 0 = working (resale side)
                price_type TEXT,                   -- FIXED / MIN_BID / FAST_BID / ...
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                last_bid_check_utc TEXT,
                status TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'gone'
                closed_utc TEXT,
                final_ask_cents INTEGER,
                final_bid_cents INTEGER,           -- highest bid ever observed
                bid_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Append-only price observations; a row is added only when the ask
        # or bid situation actually changed, so it stays small.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_obs (
                listing_id TEXT NOT NULL,
                ts_utc TEXT NOT NULL,
                ask_cents INTEGER,
                highest_bid_cents INTEGER,
                bid_count INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_obs_listing ON price_obs(listing_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_model ON market_listings(model, is_damaged, status)"
        )
        conn.commit()
    logger.info("Database ready at %s", db_path)


def get_cached_coords(place_name: str, db_path: Path = DB_PATH):
    """Return (lat, lon) if we've geocoded this place before, else None."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT lat, lon FROM geocode_cache WHERE place_name = ?", (place_name,)
        ).fetchone()
    return (row[0], row[1]) if row else None


def cache_coords(place_name: str, lat: float, lon: float, db_path: Path = DB_PATH) -> None:
    """Save a geocoded place so future runs never look it up again."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO geocode_cache (place_name, lat, lon) VALUES (?, ?, ?)",
            (place_name, lat, lon),
        )
        conn.commit()


def get_seen_record(listing_id: str, db_path: Path = DB_PATH):
    """Return {"matched", "first_seen_utc", "last_seen_utc"} if we've processed
    this listing before, else None."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT matched, first_seen_utc, last_seen_utc FROM seen_listings WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()
    if row is None:
        return None
    return {"matched": bool(row[0]), "first_seen_utc": row[1], "last_seen_utc": row[2]}


def mark_seen(
    listing_id: str,
    title: str,
    url: str,
    matched: bool,
    db_path: Path = DB_PATH,
) -> None:
    """
    Record a listing as processed - whether it matched our filters or not.
    We record non-matches too, so we don't waste time/tokens re-evaluating
    the same irrelevant listing every single run.
    """
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO seen_listings
                (listing_id, title, url, matched, first_seen_utc, last_seen_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (listing_id, title, url, int(matched), now, now),
        )
        conn.commit()


def touch_last_seen(listing_id: str, db_path: Path = DB_PATH) -> None:
    """Update last_seen_utc to now for a listing we've encountered again."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE seen_listings SET last_seen_utc = ? WHERE listing_id = ?",
            (datetime.now(timezone.utc).isoformat(), listing_id),
        )
        conn.commit()


def check_reappeared(listing_id: str, gap_hours: float, db_path: Path = DB_PATH) -> bool:
    """
    Return True if this listing was last seen more than `gap_hours` ago.
    Since each scan only pulls the newest-30 results per query, a listing
    that drops out of view has been sold/removed/pushed off the list - if
    it later resurfaces, that's a relist/bump, not the same scan re-finding
    it, and is worth treating as a fresh opportunity again.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_seen_utc FROM seen_listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
    if row is None or row[0] is None:
        return False
    gap = datetime.now(timezone.utc) - datetime.fromisoformat(row[0])
    return gap.total_seconds() > gap_hours * 3600


def count_seen(db_path: Path = DB_PATH) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()
    return row[0] if row else 0
