"""Photo attachment for the AI classifier (2026-08-19).

Sellers routinely describe the damage as "zie foto's" and name nothing, so
the classifier reads the listing pictures. These tests pin the behaviour that
must never regress: a photo problem must degrade to a text-only verdict, never
sink the listing.
"""

import ai_classifier
import scraper


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
