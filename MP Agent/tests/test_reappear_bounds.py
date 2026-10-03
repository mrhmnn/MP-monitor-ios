"""Bounds on the reappear re-alert (2026-10-03).

Milad: "I don't like reappeared after being online messages... if it's 1 day
or 2 days after its first appearance I want to see it, but make sure that
doesn't happen more than 1 time; also stop giving me all the alerts at once,
I kept getting 20 alerts of reappeared models in a minute span."

The old rule was one condition: last seen more than 24h ago and it matched
once. No upper age bound, no once-only flag, no per-run cap - so a listing
could re-alert every time it bounced back into the newest-30 window, months
after it was first seen, and a batch of them could land together.

Measured before the change: 1 re-alert per 12 new matches over 75 consecutive
production runs, so this is a small slice of volume - but it is a DUPLICATE
slice, every one of them a phone he had already been shown.
"""

from datetime import datetime, timedelta, timezone

import pytest

import storage


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    storage.init_db(path)
    return path


def _seen(db, listing_id, first_seen_days_ago, last_seen_hours_ago, matched=True):
    now = datetime.now(timezone.utc)
    first = (now - timedelta(days=first_seen_days_ago)).isoformat()
    last = (now - timedelta(hours=last_seen_hours_ago)).isoformat()
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO seen_listings
                (listing_id, title, url, matched, first_seen_utc, last_seen_utc)
            VALUES (?, 't', 'u', ?, ?, ?)
            """,
            (listing_id, int(matched), first, last),
        )
        conn.commit()


class TestAgeWindow:
    def test_the_case_he_wants_still_fires(self, db):
        """First seen 2 days ago, off the radar for 30h - exactly the
        "1 day or 2 days after its first appearance" case."""
        _seen(db, "m1", first_seen_days_ago=2, last_seen_hours_ago=30)
        assert storage.check_reappeared("m1", 24, max_age_days=3, db_path=db)

    def test_an_old_listing_never_fires_again(self, db):
        """The hole that had no bound at all: first seen in July, back in
        October, still re-alerting."""
        _seen(db, "m1", first_seen_days_ago=90, last_seen_hours_ago=72)
        assert not storage.check_reappeared("m1", 24, max_age_days=3, db_path=db)

    def test_just_past_the_window_does_not_fire(self, db):
        _seen(db, "m1", first_seen_days_ago=4, last_seen_hours_ago=30)
        assert not storage.check_reappeared("m1", 24, max_age_days=3, db_path=db)

    def test_still_inside_the_window_is_not_a_reappearance(self, db):
        """Seen an hour ago - it never left, it is just still in the
        newest-30 of some query."""
        _seen(db, "m1", first_seen_days_ago=2, last_seen_hours_ago=1)
        assert not storage.check_reappeared("m1", 24, max_age_days=3, db_path=db)

    def test_no_age_bound_keeps_the_old_behaviour(self, db):
        _seen(db, "m1", first_seen_days_ago=90, last_seen_hours_ago=72)
        assert storage.check_reappeared("m1", 24, max_age_days=None, db_path=db)


class TestOnceOnly:
    def test_a_listing_re_alerts_at_most_once_ever(self, db):
        _seen(db, "m1", first_seen_days_ago=2, last_seen_hours_ago=30)
        assert storage.check_reappeared("m1", 24, max_age_days=3, db_path=db)

        storage.mark_reappear_alerted("m1", db_path=db)

        assert not storage.check_reappeared("m1", 24, max_age_days=3, db_path=db)

    def test_the_mark_survives_further_bouncing(self, db):
        """It bounces out and back a second time, still inside the age
        window - previously this re-alerted again."""
        _seen(db, "m1", first_seen_days_ago=1, last_seen_hours_ago=25)
        storage.mark_reappear_alerted("m1", db_path=db)
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE seen_listings SET last_seen_utc = ? WHERE listing_id = 'm1'",
                ((datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(),),
            )
            conn.commit()
        assert not storage.check_reappeared("m1", 24, max_age_days=3, db_path=db)


class TestMigrationIsSafe:
    def test_rows_written_before_the_column_existed_still_work(self, db):
        """reappear_alerted_utc is NULL on every pre-existing row (19,681 of
        them live). NULL must read as "not yet alerted", not as a crash."""
        _seen(db, "m1", first_seen_days_ago=2, last_seen_hours_ago=30)
        import sqlite3

        with sqlite3.connect(db) as conn:
            value = conn.execute(
                "SELECT reappear_alerted_utc FROM seen_listings WHERE listing_id='m1'"
            ).fetchone()[0]
        assert value is None
        assert storage.check_reappeared("m1", 24, max_age_days=3, db_path=db)

    def test_init_db_is_idempotent(self, db):
        storage.init_db(db)
        storage.init_db(db)   # the ALTER must not run twice


# --- The burst guard, through the real scan cycle ---------------------------


@pytest.fixture
def scan(tmp_path, monkeypatch):
    """Real run_scan_cycle with 25 previously-matched listings all coming
    back at once - the shape of the burst he reported."""
    from pathlib import Path

    import yaml

    import ai_classifier
    import distance
    import main
    import market
    import scraper
    import telegram_notifier

    from test_no_defect_gate import _isolate_db

    path = tmp_path / "test.db"
    _isolate_db(path, monkeypatch)

    config = yaml.safe_load(
        (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
    )
    config["search_queries"] = ["iphone schade"]
    config["market_queries"] = []
    config["request_delay_seconds"] = 0

    sent = []
    listings = []
    for i in range(25):
        lid = f"m{i}"
        _seen(path, lid, first_seen_days_ago=2, last_seen_hours_ago=30)
        listings.append(
            scraper.Listing(
                listing_id=lid,
                title="iPhone 16 Pro 256GB scherm kapot",
                description_snippet="scherm kapot",
                price_text="EUR 400",
                location_text="Veenendaal",
                url=f"https://www.marktplaats.nl/v/{lid}",
                price_cents=40000,
            )
        )

    monkeypatch.setattr(scraper, "fetch_listings", lambda *a, **kw: listings)
    monkeypatch.setattr(
        scraper, "fetch_listing_details", lambda *a, **kw: scraper.ListingDetails()
    )
    monkeypatch.setattr(
        telegram_notifier,
        "send_listing",
        lambda image, message, **kw: sent.append(message) or True,
    )
    monkeypatch.setattr(telegram_notifier, "send_message", lambda *a, **kw: True)
    monkeypatch.setattr(
        distance,
        "get_driving_distance_from_coords",
        lambda *a, **kw: distance.DistanceResult(10, 12, "driving", "OK"),
    )
    for name in ("ingest_listings", "poll_bids", "check_closures"):
        monkeypatch.setattr(market, name, lambda *a, **kw: None)
    monkeypatch.setattr(market, "benchmark_line", lambda *a, **kw: "")
    monkeypatch.setattr(market, "deal_line", lambda *a, **kw: "")

    return {"config": config, "sent": sent, "main": main, "db": path}


def test_twenty_five_at_once_is_capped(scan):
    """The reported failure: ~20 reappear alerts inside a minute."""
    scan["main"].run_scan_cycle(scan["config"])
    assert len(scan["sent"]) == scan["config"]["reappear_max_alerts_per_run"] == 2


def test_the_rest_are_not_merely_deferred_to_the_next_run(scan):
    """A cap that just spreads the burst over the following runs would hand
    him the same 25 messages, only slower. The ones over the cap are never
    marked, but their last_seen IS touched, so they fall out of the
    gap window instead of queueing up."""
    scan["main"].run_scan_cycle(scan["config"])
    scan["sent"].clear()
    scan["main"].run_scan_cycle(scan["config"])
    assert scan["sent"] == []


def test_the_switch_kills_the_path_entirely(scan):
    scan["config"]["realert_on_reappear"] = False
    scan["main"].run_scan_cycle(scan["config"])
    assert scan["sent"] == []
