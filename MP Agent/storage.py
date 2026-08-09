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
        # The accept/reject reason (2026-07-13). Diagnosing "why didn't I get
        # an alert for X" used to require replaying the whole pipeline; now
        # it's one SELECT. NULL on rows from before this column existed.
        if "reason" not in existing_columns:
            conn.execute("ALTER TABLE seen_listings ADD COLUMN reason TEXT")
        # What ACTUALLY happened to this listing (2026-07-28). `matched` is
        # written before the distance check and the Telegram send, and no
        # later step corrects it - so a listing 222 km away, or one whose
        # send failed, is stored identically to one that reached the phone.
        # Every "alerts per day" figure taken from `matched` is therefore an
        # AI-accept count, not a delivery count. This column records the end
        # of the pipeline instead of the middle of it:
        #   'rejected' | 'accepted' | 'too_far' | 'alerted' | 'send_failed'
        # `matched` is deliberately left alone - dedup and the reappear path
        # both key off it, and changing its meaning would alter behaviour.
        if "outcome" not in existing_columns:
            conn.execute("ALTER TABLE seen_listings ADD COLUMN outcome TEXT")
        # Cross-run counters for the scan health check (2026-07-28). Lives in
        # the DB because that's the only thing that survives between GitHub
        # Actions runs - the workflow pushes it to the `data` branch each run.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_health (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
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
        # Bargain-alert dedup (2026-07-23). Kept here rather than in
        # seen_listings on purpose - see market.mark_bargain_alerted.
        market_columns = {row[1] for row in conn.execute("PRAGMA table_info(market_listings)")}
        if "bargain_alerted_utc" not in market_columns:
            conn.execute("ALTER TABLE market_listings ADD COLUMN bargain_alerted_utc TEXT")
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
    reason: str = "",
    db_path: Path = DB_PATH,
) -> None:
    """
    Record a listing as processed - whether it matched our filters or not.
    We record non-matches too, so we don't waste time/tokens re-evaluating
    the same irrelevant listing every single run. `reason` is the filter/AI
    decision text, stored so misses can be diagnosed with one query.

    The initial `outcome` is the decision so far ('accepted'/'rejected'); an
    accepted listing still has to clear the distance check and the Telegram
    send, and set_outcome() records how that ended.
    """
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO seen_listings
                (listing_id, title, url, matched, first_seen_utc, last_seen_utc,
                 reason, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id, title, url, int(matched), now, now, reason,
                "accepted" if matched else "rejected",
            ),
        )
        conn.commit()


def set_outcome(
    listing_id: str,
    outcome: str,
    reason: str | None = None,
    db_path: Path = DB_PATH,
) -> None:
    """
    Record what actually happened at the END of the pipeline: 'alerted',
    'too_far', or 'send_failed'. Without this, `matched` is the only signal
    available and it says "the AI said yes", which is not the same thing as
    "it reached his phone" - the gap is every listing outside the radius.

    `reason` is appended to (not replaced), so the accept reason that got the
    listing this far is preserved alongside the reason it stopped here.
    """
    with sqlite3.connect(db_path) as conn:
        if reason is None:
            conn.execute(
                "UPDATE seen_listings SET outcome = ? WHERE listing_id = ?",
                (outcome, listing_id),
            )
        else:
            conn.execute(
                """
                UPDATE seen_listings
                   SET outcome = ?,
                       reason = COALESCE(reason || ' | ', '') || ?
                 WHERE listing_id = ?
                """,
                (outcome, reason, listing_id),
            )
        conn.commit()


def bump_health_counter(key: str, failed: bool, db_path: Path = DB_PATH) -> int:
    """
    Increment a consecutive-failure counter when `failed`, reset it to 0 when
    not, and return the resulting value.

    Exists so a single blocked run doesn't fire an alarm. Marktplaats 403s the
    GitHub Actions IP for a few minutes at a time and then lets it back in
    (observed 2026-07-28: two runs fetched 0 while the runs either side of
    them fetched 793 each). A one-run outage is self-healing and needs no
    action, so alerting on it just trains the alert to be ignored.
    """
    with sqlite3.connect(db_path) as conn:
        if not failed:
            conn.execute(
                "INSERT OR REPLACE INTO scan_health (key, value) VALUES (?, 0)", (key,)
            )
            conn.commit()
            return 0
        row = conn.execute(
            "SELECT value FROM scan_health WHERE key = ?", (key,)
        ).fetchone()
        value = (row[0] if row else 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO scan_health (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()
    return value


def get_health_value(key: str, db_path=None):
    """
    Read a raw scan_health value, or None if the key was never written.

    db_path resolves at CALL time rather than defaulting to DB_PATH in the
    signature: a `db_path: Path = DB_PATH` default is bound at import, so
    monkeypatching storage.DB_PATH in a test silently has no effect and the
    test writes to the real database instead. main.py calls these without a
    path, so they need to be patchable.
    """
    db_path = db_path or DB_PATH
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM scan_health WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else None


def set_health_value(key: str, value: int, db_path=None) -> None:
    """Write a raw scan_health value (epoch seconds, counters, flags)."""
    db_path = db_path or DB_PATH
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scan_health (key, value) VALUES (?, ?)",
            (key, int(value)),
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
