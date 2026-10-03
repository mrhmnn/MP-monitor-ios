"""Photo attachment for the AI classifier (2026-08-19).

Sellers routinely describe the damage as "zie foto's" and name nothing, so
the classifier reads the listing pictures. These tests pin the behaviour that
must never regress: a photo problem must degrade to a text-only verdict, never
sink the listing.

TestGalleryNotThumbnail covers the 2026-10-03 audit finding: the live scan was
passing the SEARCH-result image list, and the LRP API returns exactly one
picture per item (measured live, 30 of 30), so ai_max_images: 3 had always
been effectively 1 and the crack on photo 3 was never seen.
"""

from pathlib import Path

import pytest
import yaml

import ai_classifier
import distance
import main
import market
import scraper
import telegram_notifier

from test_no_defect_gate import _isolate_db


class TestImageBlocks:
    def test_no_images_returns_empty(self):
        assert ai_classifier._image_blocks(None, 3) == []
        assert ai_classifier._image_blocks([], 3) == []

    def test_unreachable_image_is_skipped_not_raised(self):
        # A dead photo URL must not raise - a text-only verdict is still far
        # better than losing the listing to an exception.
        blocks = ai_classifier._image_blocks(
            ["https://images.marktplaats.com/does-not-exist-abcdef.jpg"], 3
        )
        assert blocks == []

    def test_max_images_is_respected(self):
        # Cap is what keeps a vision call on every ambiguous listing cheap.
        urls = ["https://images.marktplaats.com/nope-%d.jpg" % i for i in range(10)]
        assert len(ai_classifier._image_blocks(urls, 0)) == 0


class TestListingCarriesImages:
    def test_listing_defaults_to_empty_image_list(self):
        listing = scraper.Listing(
            listing_id="m1", title="t", description_snippet="", price_text="",
            location_text="", url="u",
        )
        assert listing.image_urls == []

    def test_image_urls_are_independent_per_listing(self):
        # A mutable default would share one list across every listing.
        a = scraper.Listing("m1", "t", "", "", "", "u")
        b = scraper.Listing("m2", "t", "", "", "", "u")
        a.image_urls.append("x")
        assert b.image_urls == []


THUMBNAIL = "https://images.marktplaats.com/api/v1/x/thumb?rule=ecg_mp_eps$_83.jpg"
GALLERY = [
    f"https://images.marktplaats.com/api/v1/x/g{i}?rule=ecg_mp_eps$_#.jpg"
    for i in range(4)
]


@pytest.fixture
def scan(tmp_path, monkeypatch):
    """The real scan cycle, capturing what the classifier was handed."""
    _isolate_db(tmp_path / "test.db", monkeypatch)

    config = yaml.safe_load(
        (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
    )
    config["search_queries"] = ["iphone schade"]
    config["market_queries"] = []
    config["request_delay_seconds"] = 0

    state = {"config": config, "handed": None, "details": None}

    def _classify(text, model, high_value=False, image_urls=None, max_images=3):
        state["handed"] = image_urls
        return ai_classifier.AiVerdict(relevant=True, reason="cracked back glass")

    monkeypatch.setattr(ai_classifier, "classify_ambiguous_listing", _classify)
    monkeypatch.setattr(
        scraper, "fetch_listing_details", lambda *a, **kw: state["details"]
    )
    monkeypatch.setattr(telegram_notifier, "send_listing", lambda *a, **kw: True)
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

    def run(details, description="Toestel heeft schade, zie de foto's."):
        state["details"] = details
        listing = scraper.Listing(
            listing_id="m-gallery",
            title="iPhone 16 Pro 256GB",
            description_snippet=description,
            price_text="€ 400,00",
            location_text="Veenendaal",
            url="https://www.marktplaats.nl/v/m-gallery",
            price_cents=40000,
            image_url=THUMBNAIL,
            image_urls=[THUMBNAIL],
        )
        monkeypatch.setattr(scraper, "fetch_listings", lambda *a, **kw: [listing])
        main.run_scan_cycle(config)
        return state["handed"]

    state["run"] = run
    return state


class TestGalleryNotThumbnail:
    def test_the_detail_page_gallery_is_what_reaches_the_classifier(self, scan):
        handed = scan["run"](
            scraper.ListingDetails(
                description="Toestel heeft schade, zie de foto's.",
                image_urls=list(GALLERY),
            )
        )
        assert handed == GALLERY       # not [THUMBNAIL]
        assert len(handed) > 1         # the whole point of ai_max_images: 3

    def test_falls_back_to_the_thumbnail_when_the_detail_fetch_failed(self, scan):
        # fetch_listing_details returns a default ListingDetails on any
        # failure - one photo still beats none.
        handed = scan["run"](scraper.ListingDetails(description=""))
        assert handed == [THUMBNAIL]

    def test_no_photos_when_the_seller_named_the_damage(self, scan):
        # Unchanged from 08-20: images are pure token cost when the text
        # already says what is broken.
        handed = scan["run"](
            scraper.ListingDetails(
                description="Achterkant gebarsten, scherm perfect.",
                image_urls=list(GALLERY),
            ),
            description="Achterkant gebarsten, scherm perfect.",
        )
        assert handed is None
