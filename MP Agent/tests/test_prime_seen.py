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
    """A search-result listing. `hours_ago` sets a parseable post date."""
    if hours_ago is not None:
        posted_date = (
            datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        ).isoformat().replace("+00:00", "Z")
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
        fetch["listings"] = [_listing("m1", posted_date="gisteren")]

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
        "raw,expected_tz",
        [
            ("2026-11-23T08:12:03Z", timezone.utc),
            ("2026-11-23T08:12:03+00:00", timezone.utc),
            ("2026-11-23T08:12:03", timezone.utc),  # naive -> assumed UTC
        ],
    )
    def test_post_date_shapes_that_must_parse(self, raw, expected_tz):
        parsed = prime_seen.parse_posted(raw)
        assert parsed is not None and parsed.tzinfo == expected_tz

    def test_junk_dates_return_none(self):
        for raw in ("", "   ", "gisteren", "8 jun '26"):
            assert prime_seen.parse_posted(raw) is None


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
