"""
Tests for the pipeline-outcome column and the health-alert hysteresis
(both added 2026-07-28).

Background for anyone reading this later: on 07-28 the DB said one listing
"matched" that morning, and Milad's Telegram had received nothing. Both
statements were true - the listing was 222 km away and correctly skipped,
but `matched` is written before the distance check and nothing corrected it
afterwards. Every alerts-per-day figure ever taken from that column was
really an AI-accept count. The `outcome` column exists so the end of the
pipeline is recorded, not just the middle.
"""

import sqlite3

import pytest

import storage


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    storage.init_db(path)
    return path


def _row(db, listing_id):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT matched, reason, outcome FROM seen_listings WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()


# --- outcome column -------------------------------------------------------

def test_mark_seen_records_initial_outcome(db):
    storage.mark_seen("a1", "iPhone 16 Pro", "http://x", True, "AI review: cracked", db)
    storage.mark_seen("a2", "iPhone 12", "http://y", False, "not a target model", db)

    assert _row(db, "a1")[2] == "accepted"
    assert _row(db, "a2")[2] == "rejected"


def test_too_far_listing_is_not_recorded_as_an_alert(db):
    """The real 07-28 case: m2424896822, 222 km, stored as matched=1."""
    storage.mark_seen(
        "m2424896822", "iPhone 15 Pro Max 256GB Natural Titanium", "http://x",
        True, "AI review: Cracked back cover is a cheap glass swap.", db,
    )
    storage.set_outcome("m2424896822", "too_far", "too far - 222 km (max 200 km)", db)

    matched, reason, outcome = _row(db, "m2424896822")
    assert outcome == "too_far"
    # The accept reason survives - both halves of the story are needed to
    # diagnose a miss, so set_outcome appends rather than overwrites.
    assert "Cracked back cover" in reason
    assert "222 km" in reason
    # matched is deliberately untouched: dedup and the reappear path key off
    # it, and this change must not alter delivery behaviour.
    assert matched == 1


def test_sent_and_failed_alerts_are_distinguishable(db):
    storage.mark_seen("s1", "iPhone 17 Pro", "http://x", True, "AI review: screen", db)
    storage.mark_seen("s2", "iPhone 17 Pro Max", "http://y", True, "AI review: screen", db)

    storage.set_outcome("s1", "alerted", db_path=db)
    storage.set_outcome("s2", "send_failed", "ALERT LOST - Telegram send failed", db)

    assert _row(db, "s1")[2] == "alerted"
    assert _row(db, "s2")[2] == "send_failed"
    assert "ALERT LOST" in _row(db, "s2")[1]


def test_outcome_counts_separate_delivered_from_accepted(db):
    """The query that was impossible before: how many actually landed?"""
    for i, outcome in enumerate(["alerted", "alerted", "too_far", "send_failed"]):
        storage.mark_seen(f"n{i}", "iPhone 16 Pro Max", "http://x", True, "AI", db)
        storage.set_outcome(f"n{i}", outcome, db_path=db)

    with sqlite3.connect(db) as conn:
        delivered = conn.execute(
            "SELECT count(*) FROM seen_listings WHERE outcome = 'alerted'"
        ).fetchone()[0]
        accepted = conn.execute(
            "SELECT count(*) FROM seen_listings WHERE matched = 1"
        ).fetchone()[0]

    assert accepted == 4
    assert delivered == 2


def test_outcome_migration_is_additive_on_an_existing_db(db):
    """init_db must be safe to re-run against a DB that predates the column -
    the live DB on the `data` branch has ~4,100 rows written before it
    existed, and they have to survive untouched."""
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("ALTER TABLE ... DROP COLUMN needs SQLite 3.35+")
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE seen_listings DROP COLUMN outcome")
        conn.execute(
            "INSERT INTO seen_listings (listing_id, title, url, matched,"
            " first_seen_utc, last_seen_utc) VALUES ('old', 't', 'u', 1, 'x', 'y')"
        )
        conn.commit()

    storage.init_db(db)

    assert _row(db, "old") == (1, None, None)
    storage.set_outcome("old", "alerted", db_path=db)
    assert _row(db, "old")[2] == "alerted"


# --- health hysteresis ----------------------------------------------------

def test_single_blocked_run_does_not_reach_the_alert_threshold(db):
    """A 403 blip: one bad run between two good ones must stay silent."""
    assert storage.bump_health_counter("unhealthy_runs", False, db) == 0
    assert storage.bump_health_counter("unhealthy_runs", True, db) == 1
    assert storage.bump_health_counter("unhealthy_runs", False, db) == 0


def test_sustained_breakage_reaches_the_threshold(db):
    streaks = [storage.bump_health_counter("unhealthy_runs", True, db) for _ in range(3)]
    assert streaks == [1, 2, 3]


def test_counter_resets_after_recovery(db):
    for _ in range(5):
        storage.bump_health_counter("unhealthy_runs", True, db)
    storage.bump_health_counter("unhealthy_runs", False, db)
    assert storage.bump_health_counter("unhealthy_runs", True, db) == 1


def test_counter_survives_a_fresh_connection(db):
    """It has to: each GitHub Actions run is a new process, and the DB is
    the only state carried across them (pushed to the `data` branch)."""
    storage.bump_health_counter("unhealthy_runs", True, db)
    storage.bump_health_counter("unhealthy_runs", True, db)
    storage.init_db(db)  # simulates the next run starting up
    assert storage.bump_health_counter("unhealthy_runs", True, db) == 3
