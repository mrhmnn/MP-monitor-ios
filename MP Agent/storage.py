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
                first_seen_utc TEXT NOT NULL
            )
            """
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


def is_seen(listing_id: str, db_path: Path = DB_PATH) -> bool:
    """Return True if we've already processed this listing before."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
    return row is not None


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
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO seen_listings (listing_id, title, url, matched, first_seen_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (listing_id, title, url, int(matched), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def count_seen(db_path: Path = DB_PATH) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()
    return row[0] if row else 0
