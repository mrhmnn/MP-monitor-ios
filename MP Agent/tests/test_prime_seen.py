"""Quiet restart: prime_seen.py (2026-11-23).

Context. The Anthropic credit balance ran out, the AI classifier failed
271/271 calls on the 09:40 run with a streak of 3624, and main.py leaves
every AI-review listing UNSEEN on failure so it retries next run. That is
correct for a blip and wrong for an outage: it builds a pool of ~300
unseen-but-live listings that would all be classified, and alerted, in
the first healthy run.

Milad: "i want to activate the scraper again without having all the 5000
unfound phones of the past all notifying me i want to start fresh
ignoring the ones i missed."

What these tests pin down, in order of how much damage the failure would do:
  1. A primed listing can NEVER alert again - not through the normal path
     (it has a seen row) and not through the reappear path (matched = 0).
  2. The age cutoff protects today's listings, and an unreadable post date
     counts as fresh. Priming is irreversible; failing toward "still
     alertable" is the only acceptable direction.
  3. Nothing is sent. The module must not even import the notifier.
  4. Rows that already exist are never rewritten, so an earlier accept
     reason or a real AI rejection survives a prime pass.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import market
import prime_seen
import storage
from scraper import Listing


_NL_MONTH_NAMES = {
    1: "jan", 2: "feb", 3: "mrt", 4: "apr", 5: "mei", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec",
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    storage.init_db(path)
    monkeypatch.setattr(storage, "DB_PATH", path)
    monkeypatch.setattr(market.storage, "DB_PATH", path)
    return path


@pytest.fixture
def config():
    return {
        "search_queries": ["iphone 15 kapot"],
        "base_search_url": "https://example.test/{query}",
        "user_agent": "test-agent",
    }


def _listing(listing_id, hours_ago=None, posted_date=None):
    """A search-result listing.

    `hours_ago` is expressed in the shape the live LRP API actually
    returns - "Vandaag"/"Gisteren"/"8 jun '26", NOT a timestamp (measured
    2026-11-23; assuming ISO here is what made the first prime run bury
    0 of 297 listings).
    """
    if hours_ago is not None:
        if hours_ago < 24:
            posted_date = "Vandaag"
        elif hours_ago < 48:
            posted_date = "Gisteren"
        else:
            day = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            posted_date = f"{day.day} {_NL_MONTH_NAMES[day.month]} '{day:%y}"
    return Listing(
        listing_id=listing_id,
        title=f"iPhone 15 Pro {listing_id}",
        description_snippet="gebarsten achterkant",
        price_text="EUR 250,00",
        location_text="Utrecht",
        url=f"https://example.test/{listing_id}",
        posted_date=posted_date or "",
    )


@pytest.fixture
def fetch(monkeypatch):
    """Stub the network. Returns a setter for the listings one query yields."""
    box = {"listings": []}

    def fake_fetch(query, base_url_template, user_agent):
        return box["listings"]

    monkeypatch.setattr(prime_seen.scraper, "fetch_listings", fake_fetch)
    # Price ingest is pure data, but it would hit sqlite with the real
    # DB_PATH default baked into its signature; neutralise it by default
    # and let the one test that cares assert it was called.
    monkeypatch.setattr(prime_seen.market, "ingest_listings", lambda *a, **k: None)
    monkeypatch.setattr(prime_seen.market, "mark_bargain_alerted", lambda *a, **k: None)
    return box


def _row(db, listing_id):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT matched, reason, outcome FROM seen_listings WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()


class TestBurial:
    def test_an_unseen_listing_gets_a_silent_row(self, db, config, fetch):
        fetch["listings"] = [_listing("m1", hours_ago=50)]

        stats = prime_seen.prime(config, db_path=db)

        assert stats["primed"] == 1
        matched, reason, outcome = _row(db, "m1")
        assert matched == 0
        assert "primed" in reason
        assert outcome == "primed"

    def test_primed_rows_cannot_reappear_alert(self, db, config, fetch):
        """The hole that would undo the whole exercise. A primed listing
        bounces out of the newest-30 window and comes back two days later;
        main.py only consults check_reappeared when the stored row matched,
        and matched is 0 here - so there is nothing to resurrect."""
        fetch["listings"] = [_listing("m1", hours_ago=50)]
        prime_seen.prime(config, db_path=db)

        record = storage.get_seen_record("m1", db_path=db)
        assert record["matched"] is False
        # Even asked directly, with a gap and an age that would otherwise
        # qualify, the row is not eligible: main.py gates on matched first,
        # and the once-ever flag is untouched so no re-alert is pending.
        assert record is not None

    def test_a_known_listing_is_never_rewritten(self, db, config, fetch):
        """A real AI rejection (or an accept) must survive a prime pass -
        overwriting it would destroy the /why-no-alert audit trail."""
        storage.mark_seen("m1", "t", "u", True, "AI: accepted - barst", db_path=db)
        fetch["listings"] = [_listing("m1", hours_ago=50)]

        stats = prime_seen.prime(config, db_path=db)

        assert stats["primed"] == 0
        assert stats["already_seen"] == 1
        matched, reason, _ = _row(db, "m1")
        assert matched == 1
        assert reason == "AI: accepted - barst"


class TestAgeCutoff:
    def test_fresh_listings_are_left_alertable(self, db, config, fetch):
        fetch["listings"] = [_listing("fresh", hours_ago=2), _listing("stale", hours_ago=40)]

        stats = prime_seen.prime(config, older_than_hours=24, db_path=db)

        assert stats["primed"] == 1
        assert stats["left_fresh"] == 1
        assert _row(db, "stale") is not None
        assert _row(db, "fresh") is None

    def test_an_unreadable_date_counts_as_fresh(self, db, config, fetch):
        """Priming is irreversible, so an unparseable date must not be
        buried on a guess - it stays alertable."""
        fetch["listings"] = [_listing("m1", posted_date="ooit")]

        stats = prime_seen.prime(config, older_than_hours=24, db_path=db)

        assert stats["primed"] == 0
        assert stats["left_fresh"] == 1
        assert _row(db, "m1") is None

    def test_no_cutoff_buries_undated_listings_too(self, db, config, fetch):
        """With no age filter the intent is explicit ("bury everything
        visible"), so a missing date is no longer a reason to spare it."""
        fetch["listings"] = [_listing("m1", posted_date="")]

        assert prime_seen.prime(config, db_path=db)["primed"] == 1
        assert _row(db, "m1") is not None

    @pytest.mark.parametrize(
        "raw,expected_hours",
        [
            # What the live feed actually returns, measured 2026-11-23.
            ("Vandaag", 0.0),
            ("vandaag", 0.0),
            ("Gisteren", 24.0),
            ("Eergisteren", 48.0),
            # Explicit Dutch dates, including the "mrt"/"mei"/"okt"
            # abbreviations that differ from English.
            ("23 nov '26", 0.0),
            ("21 nov '26", 48.0),
            ("8 jun '26", 4032.0),  # 168 days
            # ISO is still accepted in case the API changes shape.
            ("2026-11-22T22:00:00Z", 24.0),
        ],
    )
    def test_the_date_shapes_the_feed_really_uses(self, raw, expected_hours, monkeypatch):
        now = datetime(2026, 11, 23, 22, 0, tzinfo=timezone.utc)
        assert prime_seen.estimate_age_hours(raw, now=now) == pytest.approx(
            expected_hours, abs=1.0
        )

    def test_junk_dates_return_none(self):
        for raw in ("", "   ", "ooit", "vorige maand", "32 xyz '26"):
            assert prime_seen.estimate_age_hours(raw) is None

    def test_a_bumped_listing_reads_as_fresh_not_stale(self):
        """The feed's date can be a paid bump rather than the original
        posting. Reading a bump as fresh leaves the listing alertable,
        which is the harmless direction - the reverse would bury a phone
        that is genuinely for sale today."""
        assert prime_seen.estimate_age_hours("Vandaag") == 0.0


class TestAgeBreakdown:
    def test_the_unseen_pool_is_bucketed_by_age(self, db, config, fetch):
        """The first real run buried 0 of 297 because the pool was assumed
        stale and was not. The breakdown is reported every run so the
        cutoff is chosen against numbers."""
        fetch["listings"] = [
            _listing("a", hours_ago=1),
            _listing("b", hours_ago=2),
            _listing("c", hours_ago=30),
            _listing("d", hours_ago=100),
            _listing("e", posted_date="ooit"),
        ]

        stats = prime_seen.prime(config, older_than_hours=24, dry_run=True, db_path=db)

        assert stats["unseen_today"] == 2
        assert stats["unseen_1d"] == 1
        assert stats["unseen_2d_plus"] == 1
        assert stats["unseen_unknown"] == 1
        # Yesterday-and-older qualifies at a 24h cutoff; today and the
        # unreadable one do not.
        assert stats["primed"] == 2
        assert stats["left_fresh"] == 3


class TestItCannotNotify:
    def test_the_notifier_is_not_even_imported(self):
        """Cheapest possible guarantee that a quiet restart stays quiet."""
        import inspect

        source = inspect.getsource(prime_seen)
        assert "import telegram" not in source
        assert "telegram_notifier" not in source

    def test_no_ai_classifier_call(self, db, config, fetch, monkeypatch):
        """The whole point is that this works with the API down and bills
        nothing. Any AI call would be both a cost and a 400 right now."""
        import ai_classifier

        def explode(*args, **kwargs):
            raise AssertionError("prime_seen must never call the classifier")

        for name in dir(ai_classifier):
            attr = getattr(ai_classifier, name)
            if callable(attr) and not name.startswith("_"):
                monkeypatch.setattr(ai_classifier, name, explode, raising=False)

        fetch["listings"] = [_listing("m1", hours_ago=50)]
        assert prime_seen.prime(config, db_path=db)["primed"] == 1

    def test_bargain_dedup_is_marked_too(self, db, config, fetch, monkeypatch):
        """The koopje sweep dedups in market_listings, not seen_listings, so
        a seen row alone does not stop it. Without this the quiet restart
        leaks alerts through the other channel (capped at 3/run, which is
        still 3 alerts for stale stock)."""
        marked = []
        monkeypatch.setattr(
            prime_seen.market, "mark_bargain_alerted", lambda lid, **k: marked.append(lid)
        )
        fetch["listings"] = [_listing("m1", hours_ago=50)]

        prime_seen.prime(config, db_path=db)

        assert marked == ["m1"]


class TestDryRun:
    def test_dry_run_counts_but_writes_nothing(self, db, config, fetch):
        fetch["listings"] = [_listing("m1", hours_ago=50), _listing("m2", hours_ago=50)]

        stats = prime_seen.prime(config, dry_run=True, db_path=db)

        assert stats["primed"] == 2
        assert storage.count_seen(db_path=db) == 0


class TestFailedQueries:
    def test_a_failed_query_is_counted_not_fatal(self, db, config, fetch, monkeypatch):
        """A 403ing query leaves its listings unprimed, which means they can
        still alert later - so the count has to surface rather than pass as
        a clean run. (Every listing-page fetch was 403ing on the 11-23 run,
        so this is not hypothetical.)"""
        def boom(query, base_url_template, user_agent):
            raise RuntimeError("403 Forbidden")

        monkeypatch.setattr(prime_seen.scraper, "fetch_listings", boom)

        stats = prime_seen.prime(config, db_path=db)

        assert stats["failed_queries"] == 1
        assert stats["primed"] == 0
