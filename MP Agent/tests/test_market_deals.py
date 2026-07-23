"""
Tests for the price-intelligence layer added 2026-07-23: deal scoring,
the bargain sweep, the negation-aware damaged/working split, and the
alert-age formatter.

The damaged/working split has its own section because a bug there is
invisible - it corrupts the benchmark every buy decision is measured
against, rather than producing a wrong alert anyone would notice.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import market
import storage
import telegram_notifier

CONFIG = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8")
)


@dataclass
class FakeListing:
    """Stand-in for scraper.Listing with only the fields these paths touch."""
    title: str
    description_snippet: str = ""
    listing_id: str = "m1"
    price_cents: int = 0
    price_type: str = "FIXED"
    condition: str = ""
    storage_text: str = ""
    url: str = "https://example.invalid/v/1"
    seller_has_website: bool = False
    priority_product: str = "NONE"
    latitude: float = None
    longitude: float = None
    location_text: str = "Utrecht"
    image_url: str = ""
    posted_date: str = ""

    @property
    def combined_text(self):
        return f"{self.title} {self.description_snippet}".lower()


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    storage.init_db(path)
    return path


def seed_candidate(db, listing):
    """Insert the market_listings row that ingest_listings would have
    written before the bargain sweep runs - dedup updates that row."""
    import sqlite3
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO market_listings
               (listing_id, model, is_damaged, title, url, first_seen_utc,
                last_seen_utc, status, final_ask_cents)
               VALUES (?,?,0,?,?,?,?, 'open', ?)""",
            (listing.listing_id, "iphone 15", listing.title, listing.url,
             now, now, listing.price_cents),
        )
        conn.commit()


def seed_working(db, model, price_eur, n, prefix="w"):
    """Insert n open WORKING listings so the benchmark has a sample."""
    import sqlite3
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO market_listings
                   (listing_id, model, is_damaged, title, url, first_seen_utc,
                    last_seen_utc, status, final_ask_cents)
                   VALUES (?,?,0,?,?,?,?, 'open', ?)""",
                (f"{prefix}{i}", model, f"{model} test", "u", now, now,
                 int(price_eur * 100)),
            )
        conn.commit()


# --- Damaged vs working split (the silent-corruption bug) -------------------

class TestDamagedSplit:
    @pytest.mark.parametrize("title", [
        # All real production rows that were misfiled as DAMAGED, pulling
        # the highest-priced clean phones out of the resale benchmark and
        # biasing it downward - see market._is_damaged.
        "iPhone 15 Pro Max 512GB | 100% Batterij geen schade + doos",
        "Iphone 15 Pro 256GB | Natural Titanium | Zonder krassen",
        "iPhone 17 Pro max ( zonder schade) %100 batterij",
        "iPhone 15 Roze zonder schade met nieuw protector glas",
        "iPhone 15 Pro Zwart 90% bat 128gb GEEN krassen",
        "Rode iPhone 15 256GB opslag, 89% batterij, geen schade",
    ])
    def test_negated_damage_is_not_damaged(self, title):
        assert market._is_damaged(FakeListing(title), CONFIG) is False

    @pytest.mark.parametrize("title", [
        "iPhone 15 scherm kapot",
        "iPhone 16 Pro achterkant gebarsten",
        "iPhone 17 met schade aan de zijkant",
        "iPhone 15 Pro voor onderdelen",
    ])
    def test_real_damage_still_detected(self, title):
        assert market._is_damaged(FakeListing(title), CONFIG) is True

    def test_negation_does_not_mask_a_real_defect(self):
        # The critical direction: a seller ruling one thing out while
        # describing another must NOT be laundered into "working".
        listing = FakeListing(
            "iPhone 15 Pro",
            "Geen waterschade en geen krassen, maar het scherm is kapot.",
        )
        assert market._is_damaged(listing, CONFIG) is True


# --- Deal scoring ------------------------------------------------------------

class TestDealScore:
    def test_cheap_listing_scores_as_topdeal(self, db):
        seed_working(db, "iphone 15", 400, 10)
        # 400 ask-median * 0.90 = 360 reference; 150 ask => 58% headroom.
        score = market.deal_score("iphone 15", 15000, CONFIG, db)
        assert score["tier"] == "🔥 TOPDEAL"
        assert score["basis"] == "vraag"

    def test_expensive_listing_scores_as_too_expensive(self, db):
        seed_working(db, "iphone 15", 400, 10)
        score = market.deal_score("iphone 15", 34000, CONFIG, db)
        assert score["tier"] == "❌ Te duur"

    def test_overpriced_listing_still_gets_a_verdict(self, db):
        # Negative headroom (asking MORE than the phone is worth working)
        # matched no tier and raised StopIteration, which was swallowed as
        # "no score" - so the worst listings silently lost their price
        # line. Found in live verification 2026-07-23.
        seed_working(db, "iphone 15", 400, 10)
        score = market.deal_score("iphone 15", 90000, CONFIG, db)
        assert score is not None and score["tier"] == "❌ Te duur"
        assert score["headroom_pct"] < 0
        line = market.deal_line("iphone 15", 90000, "FIXED", CONFIG, db)
        assert "❌ Te duur" in line

    def test_no_benchmark_means_no_score(self, db):
        # Thin samples must yield nothing rather than a confident-looking
        # number - an alert line carries authority it hasn't earned.
        seed_working(db, "iphone 15", 400, 2)
        assert market.deal_score("iphone 15", 15000, CONFIG, db) is None

    def test_missing_price_means_no_score(self, db):
        seed_working(db, "iphone 15", 400, 10)
        assert market.deal_score("iphone 15", 0, CONFIG, db) is None

    def test_unparseable_model_means_no_score(self, db):
        assert market.deal_score(None, 15000, CONFIG, db) is None

    def test_bidding_listing_gets_no_verdict(self, db):
        seed_working(db, "iphone 15", 400, 10)
        line = market.deal_line("iphone 15", 15000, "MIN_BID", CONFIG, db)
        # The "price" on a bidding listing is a minimum bid, so scoring it
        # like an asking price would be confidently wrong.
        assert "TOPDEAL" not in line
        assert line.startswith("💸 Bod vanaf")

    def test_fixed_price_listing_gets_a_verdict(self, db):
        seed_working(db, "iphone 15", 400, 10)
        line = market.deal_line("iphone 15", 15000, "FIXED", CONFIG, db)
        assert "🔥 TOPDEAL" in line and "€150" in line


# --- Bargain sweep -----------------------------------------------------------

class TestBargainSweep:
    def test_underpriced_working_phone_is_found(self, db):
        seed_working(db, "iphone 15", 400, 10)
        listings = [FakeListing("iPhone 15 128GB", listing_id="b1", price_cents=18000)]
        seed_candidate(db, listings[0])
        found = market.find_bargains(listings, CONFIG, db)
        assert len(found) == 1 and found[0][1]["headroom_pct"] > 0.35

    def test_fairly_priced_phone_is_ignored(self, db):
        seed_working(db, "iphone 15", 400, 10)
        listings = [FakeListing("iPhone 15 128GB", listing_id="b1", price_cents=34000)]
        assert market.find_bargains(listings, CONFIG, db) == []

    @pytest.mark.parametrize("title", [
        "Hoesje voor iPhone 15 Pro",
        "Screenprotector iPhone 15",
        "Originele oplader iPhone 15",
        "iPhone 15 scherm los - onderdelen",
    ])
    def test_accessories_are_vetoed(self, db, title):
        # Without this an accessory reads as a 90%-under-market phone -
        # the single most likely false positive in the whole feature.
        seed_working(db, "iphone 15", 400, 10)
        listings = [FakeListing(title, listing_id="b1", price_cents=15000)]
        assert market.find_bargains(listings, CONFIG, db) == []

    def test_cheap_listings_below_price_floor_are_vetoed(self, db):
        seed_working(db, "iphone 15", 400, 10)
        listings = [FakeListing("iPhone 15", listing_id="b1", price_cents=5000)]
        assert market.find_bargains(listings, CONFIG, db) == []

    def test_bidding_listings_are_excluded(self, db):
        # An opening bid of EUR 1 would otherwise score as a 100% bargain.
        seed_working(db, "iphone 15", 400, 10)
        listings = [FakeListing("iPhone 15", listing_id="b1",
                                price_cents=15000, price_type="MIN_BID")]
        assert market.find_bargains(listings, CONFIG, db) == []

    def test_damaged_phones_are_left_to_the_normal_pipeline(self, db):
        seed_working(db, "iphone 15", 400, 10)
        listings = [FakeListing("iPhone 15 scherm kapot", listing_id="b1",
                                price_cents=15000)]
        assert market.find_bargains(listings, CONFIG, db) == []

    def test_alert_fires_only_once_per_listing(self, db):
        seed_working(db, "iphone 15", 400, 10)
        listings = [FakeListing("iPhone 15 128GB", listing_id="b1", price_cents=18000)]
        seed_candidate(db, listings[0])
        assert len(market.find_bargains(listings, CONFIG, db)) == 1
        market.mark_bargain_alerted("b1", db)
        # Runs every ~8 minutes; without dedup this is ~180 alerts a day.
        assert market.find_bargains(listings, CONFIG, db) == []

    def test_untracked_listing_is_skipped_not_flooded(self, db):
        # No market_listings row => the dedup UPDATE would be a no-op, so
        # alerting would repeat every ~8 minutes forever. Must skip.
        seed_working(db, "iphone 15", 400, 10)
        listings = [FakeListing("iPhone 15 128GB", listing_id="ghost", price_cents=18000)]
        assert market.find_bargains(listings, CONFIG, db) == []

    def test_per_run_cap_is_enforced(self, db):
        seed_working(db, "iphone 15", 400, 10)
        listings = [
            FakeListing(f"iPhone 15 128GB nr{i}", listing_id=f"b{i}", price_cents=15000)
            for i in range(10)
        ]
        for l in listings:
            seed_candidate(db, l)
        found = market.find_bargains(listings, CONFIG, db)
        assert len(found) == CONFIG["bargain_max_alerts_per_run"]

    def test_disabled_flag_switches_the_feature_off(self, db):
        seed_working(db, "iphone 15", 400, 10)
        cfg = dict(CONFIG, bargain_alerts_enabled=False)
        listings = [FakeListing("iPhone 15 128GB", listing_id="b1", price_cents=18000)]
        seed_candidate(db, listings[0])
        assert market.find_bargains(listings, cfg, db) == []


# --- Alert age formatting ----------------------------------------------------

class TestFormatAge:
    def test_minutes(self):
        now = datetime.now(timezone.utc)
        posted = (now - timedelta(minutes=12)).isoformat()
        assert telegram_notifier.format_age(posted, now) == "12 min oud"

    def test_hours(self):
        now = datetime.now(timezone.utc)
        posted = (now - timedelta(hours=5)).isoformat()
        assert telegram_notifier.format_age(posted, now) == "5 uur oud"

    def test_days(self):
        now = datetime.now(timezone.utc)
        posted = (now - timedelta(days=6)).isoformat()
        assert telegram_notifier.format_age(posted, now) == "6 dagen oud"

    @pytest.mark.parametrize("value", ["", "not-a-date", None])
    def test_garbage_is_silently_ignored(self, value):
        # An unparseable date must drop the line, never break the alert.
        assert telegram_notifier.format_age(value) == ""


class TestMessageFormatting:
    def test_deal_line_is_included(self):
        msg = telegram_notifier.format_listing_message(
            title="iPhone 15 kapot", price_text="€150", url="https://x.invalid",
            match_reason="test", distance_km=10, duration_minutes=15,
            deal_line="💸 🔥 TOPDEAL — vraagt €150",
        )
        assert "TOPDEAL" in msg

    def test_age_is_appended_to_the_date(self):
        posted = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        msg = telegram_notifier.format_listing_message(
            title="iPhone 15", price_text="€150", url="https://x.invalid",
            match_reason="test", distance_km=10, duration_minutes=15,
            posted_date="23 jul '26", posted_iso=posted,
        )
        assert "3 uur oud" in msg and "23 jul" in msg
