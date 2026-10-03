"""What the AI review is for (2026-08-20).

Its main job is DESCRIBING the damage: the alert Milad reads on his phone
carries the AI's sentence, and the prompt is recall-biased on purpose
(unspecified damage and "voor onderdelen" both count as relevant, cost and
model-year reasoning are forbidden). Since the same day it also gates the
one case the keyword filter cannot see - a listing with no defect at all;
that half lives in test_no_defect_gate.py.

These pin the rest: photos are only read when the seller points at them,
and non-phones are stopped by the filter rather than by the AI.
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

    def test_brand_first_cases_are_accessories_too(self):
        # All four alerted as "phones" on 2026-08-20: the seller puts the
        # brand and the model in front of the accessory word and never
        # writes "voor", so the position rule alone never saw them.
        for title in (
            "Rode iPhone 15 Plus Silicone Case met MagSafe",
            "OtterBox Lumen Series Apple iPhone 15 Pro Clear Case",
            "iPhone 15 Pro Max Clear Case met MagSafe",
            "Nieuwe Pitaka Edge Case voor iPhone 16 Pro - Ongebruikt",
            # Documented as a known miss when the rule was position-only.
            "Bluebolt iPhone 17 metalen hoes",
        ):
            result = evaluate(title, "")
            assert not result.accepted, title
            assert not result.needs_ai_review, title
            assert "accessory" in result.reason, f"{title} -> {result.reason}"

    def test_a_case_thrown_in_with_a_phone_is_not_an_accessory(self):
        # The regression the brand-first rule must never cause. Both titles
        # are real listings from DB history; both sell a phone.
        for title in (
            "iPhone 13 128GB Groen - Inc Magsafe hoesje + 4 screenprotectors",
            "iPhone 15 Pro - Werkt goed, inclusief Apple leren hoesje",
            "iPhone 16 Pro kapot scherm met leren hoesje erbij",
        ):
            assert not filters.is_accessory_listing(title), title

    def test_the_cases_category_is_deliberately_ignored(self):
        # Marktplaats' own "hoesjes en frontjes" category looks like the
        # perfect free signal. Checked live 2026-08-20: eight listings in it
        # across five queries were real phones, including these two. Sellers
        # pick the category carelessly - the title is the only signal that
        # holds, so a bare-titled phone must still get judged on its text.
        for title, description in (
            ("iPhone 15", "Iphone 15 met gebroken achterkant. Doosje is niet meer aanwezig."),
            ("Iphone 15 roze", "achterkant is kapot, maar als dat wordt gemaakt is het een perfecte telefoon"),
        ):
            result = evaluate(title, description)
            assert result.accepted or result.needs_ai_review, title
            assert "accessory" not in result.reason


class TestVerifiedSellersStopAtTheFilter:
    """Marktplaats' own verified/business badge (sellerInformation.isVerified).

    Milad, 2026-08-20: "any seller which is verified/a business". Measured
    live the same day: of 346 listings from the damage queries, 11 came from
    verified sellers and every one was a trader - "Used Products", "Best Buy
    Phone", "Smartphones Emmeloord", a bulk partij seller, two wanted-ads.
    None of them had showWebsiteUrl set, so the older switch missed them all.
    """

    def test_verified_seller_is_rejected(self):
        result = evaluate(
            "iPhone 15 Pro 256GB zwart /zeer net /100% batt /garantie",
            "",
            seller_is_verified=True,
        )
        assert not result.accepted
        assert not result.needs_ai_review
        assert "verified/business seller" in result.reason

    def test_verified_beats_even_a_primary_keyword(self):
        result = evaluate(
            "iPhone 15 Pro scherm kapot", "", seller_is_verified=True
        )
        assert not result.accepted

    def test_private_seller_is_untouched(self):
        result = evaluate("iPhone 15 Pro scherm kapot", "", seller_is_verified=False)
        assert result.accepted

    def test_switch_can_be_turned_off(self):
        config = _config()
        config["reject_verified_sellers"] = False
        result = filters.evaluate_listing(
            "iPhone 15 Pro scherm kapot", "", config, seller_is_verified=True
        )
        assert result.accepted


class TestPromptMatchesConfig:
    """The prompt describes its own scope in prose, and config.yaml decides
    it. They drifted (found 2026-10-03): the iPhone 18 became a target on
    09-17, but the prompt's mechanical DECISION RULE still read "all models
    14 through 17", contradicting the four other places that said 14-18 -
    on the newest and most expensive generation in scope. The high-value
    override had drifted the same way since 07-28, naming three models by
    hand while config.yaml had grown to seven.

    These fail the next time a generation is added, instead of letting the
    contradiction sit in production for weeks.
    """

    def test_the_decision_rule_covers_every_target_generation(self):
        import ai_classifier

        generations = sorted(filters.enabled_generations(_config()["target_models"]))
        # The 14 is switched off in target_models but deliberately still
        # described as in-scope by the prompt, so compare on the TOP end:
        # that is the one that moves when a new iPhone ships.
        assert f"through {generations[-1]}" in ai_classifier.SYSTEM_PROMPT

    def test_every_scope_declaration_names_the_newest_generation(self):
        """Scoped to phrases that declare the whole SCOPE ("models 14-18",
        "models 14 through 18"). Deliberately not any "14-N" string: the
        prompt also carries real sub-ranges, like the "(14-16 gen)" battery
        and charging-port carve-out, which must not be rewritten when a new
        generation ships."""
        import re

        import ai_classifier

        newest = sorted(filters.enabled_generations(_config()["target_models"]))[-1]
        full = ai_classifier.SYSTEM_PROMPT + ai_classifier.HIGH_VALUE_SUFFIX
        declarations = re.findall(r"models 1[45] ?(?:through |-)(1[0-9])", full)
        assert declarations, "no scope declaration found in the prompt at all"
        assert set(declarations) == {newest}, (
            f"prompt declares scope up to {sorted(set(declarations))}, "
            f"config.yaml targets up to {newest}"
        )

    def test_the_high_value_override_names_no_model_list_of_its_own(self):
        """It used to hardcode "16 Pro Max / 17 Pro / 17 Pro Max". Any
        hand-maintained list here goes stale the moment config.yaml grows,
        and then tells Haiku its own listing is not high-value."""
        import ai_classifier

        models = _config()["high_value_models"]
        named = [m for m in models if m.replace("iphone ", "") in
                 ai_classifier.HIGH_VALUE_SUFFIX.lower()]
        # Describing the TIER is fine; enumerating specific models is not.
        assert len(named) <= 1, f"override enumerates models: {named}"


def _config() -> dict:
    import yaml
    from pathlib import Path

    return yaml.safe_load(
        (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
    )


def evaluate(title: str, description: str, **kwargs):
    return filters.evaluate_listing(title, description, _config(), **kwargs)
