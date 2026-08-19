"""The AI describes damage; it does not decide whether to alert (2026-08-20).

Milad: "i only want the ai review for context in the alerts to specify the
damage but alert me either way, don't make the ai review the decider."

These pin the contract that change rests on: everything reaching AI review
alerts regardless of the verdict, photos are only read when the seller points
at them, and non-phones are stopped by the filter rather than by the AI.
"""

import filters
import main


class TestPhotosOnlyWhenPointedAt:
    def test_zie_fotos_variants_trigger_photos(self):
        for text in (
            "iPhone 15 met schade, zie de foto's",
            "iPhone 15, zie foto voor de staat",
            "iPhone 16 beschadigd, op de foto te zien",
            "iPhone 15 damage, see the photos",
        ):
            assert main._POINTS_AT_PHOTOS_RE.search(text), text

    def test_named_damage_does_not_trigger_photos(self):
        # The seller already said what is broken - photos are pure token cost.
        for text in (
            "iPhone 15 scherm kapot",
            "iPhone 16 Pro achterkant gebarsten",
            "iPhone 15 voor onderdelen",
        ):
            assert not main._POINTS_AT_PHOTOS_RE.search(text), text


class TestAccessoriesStopAtTheFilter:
    def test_accessory_listings_never_reach_ai(self):
        for title in (
            "Nieuwe Screenprotector voor iPhone 16 Pro - Privacy Glas",
            "Wave Hoesje voor iPhone 15 Pro Max - Transparant",
            "JETech Tempered Glass Screen Protector voor iPhone 17 Pro",
        ):
            result = evaluate(title, "")
            assert not result.accepted, title
            assert not result.needs_ai_review, title
            assert "accessory" in result.reason, f"{title} -> {result.reason}"

    def test_accessory_without_target_model_also_stops(self):
        # Blocked a step earlier by the model filter rather than the accessory
        # rule - either way it must never reach AI review.
        result = evaluate("Oplaadkabel voor bv iPhone / 1 meter", "")
        assert not result.accepted
        assert not result.needs_ai_review

    def test_damaged_phone_sold_with_a_case_still_passes(self):
        # The regression this guard must never cause: a real target phone that
        # merely mentions an included case.
        result = evaluate("iPhone 15 met hoesje en gebarsten achterkant", "")
        assert "accessory" not in result.reason

    def test_damaged_phone_including_case_still_auto_accepts(self):
        result = evaluate("iPhone 16 Pro kapot scherm incl hoesje", "")
        assert result.accepted


def evaluate(title: str, description: str):
    import yaml
    from pathlib import Path

    config = yaml.safe_load(
        (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
    )
    return filters.evaluate_listing(title, description, config)
