"""
Unit tests for the filter decision logic - the part of the pipeline where
every historical miss has lived. Each regression case names the real
listing that motivated it.
"""

import yaml
import pytest
from pathlib import Path

import filters

CONFIG = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8")
)


def evaluate(title, description=""):
    return filters.evaluate_listing(title, description, CONFIG)


# --- Negation handling (2026-07-13 fix) -------------------------------------

class TestNegationStripping:
    def test_pure_negation_listing_is_not_sent_to_ai(self):
        # Mint-condition listing: negation only, no real damage mention.
        result = evaluate("iPhone 15 Pro in nette staat", "Geen schade, werkt perfect.")
        assert not result.accepted
        assert not result.needs_ai_review

    def test_damage_plus_unrelated_negation_still_reaches_ai(self):
        # THE bug: "geen problemen" about everything EXCEPT the damage the
        # seller just described used to suppress AI review entirely.
        result = evaluate(
            "iPhone 15 Pro", "Achterkant heeft schade, verder geen problemen."
        )
        assert result.needs_ai_review

    def test_m2420015545_regression(self):
        # Real miss 2026-07-13: 16 Pro Max, back damage + display spots,
        # must at minimum reach AI review.
        result = evaluate(
            "iPhone 16 Pro Max - 93% batterij, lichte schade",
            "De achterkant heeft lichte schade die niet storend is. Er zijn "
            "twee kleine vlekjes in het beeld die eventueel gerepareerd "
            "kunnen worden. Verder functioneert de telefoon naar",
        )
        assert result.needs_ai_review

    def test_unmatched_bucket_damage_word_survives_negation(self):
        # "kapot" is no primary keyword and no ambiguous term - unmatched
        # bucket. A trailing "geen problemen" must not block the AI look.
        result = evaluate("iPhone 15", "Glas kapot aan achterzijde, verder geen problemen.")
        assert result.needs_ai_review

    def test_unmatched_bucket_pure_negation_skips_ai(self):
        result = evaluate("iPhone 15 als nieuw", "Geen problemen, altijd hoesje om gehad.")
        assert not result.needs_ai_review

    def test_strip_negation_phrases(self):
        stripped = filters.strip_negation_phrases(
            "schade aan achterkant maar geen problemen verder",
            CONFIG["negation_phrases"],
        )
        assert "geen problemen" not in stripped
        assert "schade" in stripped


# --- Core gates --------------------------------------------------------------

class TestGates:
    def test_primary_keyword_accepts(self):
        result = evaluate("iPhone 15 scherm kapot")
        assert result.accepted

    def test_hard_exclude_rejects_even_with_primary_keyword(self):
        result = evaluate("iPhone 15 scherm kapot", "Ook waterschade gehad.")
        assert not result.accepted
        assert not result.needs_ai_review

    def test_non_target_model_rejected(self):
        result = evaluate("iPhone 12 scherm kapot")
        assert not result.accepted

    def test_vague_title_forces_ai_despite_keyword(self):
        result = evaluate("iPhone 15 Pro - lees beschrijving!", "scherm kapot")
        assert not result.accepted
        assert result.needs_ai_review

    def test_bare_model_title_with_iphone_in_description(self):
        # Production miss: "3x 17 Pro en 4x 17 256 gb met kapotte displays"
        result = evaluate(
            "3x 17 Pro en 4x 17 256 gb met kapotte displays",
            "Alle iPhones doen het verder prima.",
        )
        assert result.accepted or result.needs_ai_review

    def test_lcd_title_is_spare_part_listing(self):
        result = evaluate("iPhone 15 Pro Max scherm reparatie lcd", "voor reparatie")
        assert not result.accepted

    def test_buyer_ad_rejected(self):
        result = evaluate("Gezocht: kapotte iphone 15", "scherm kapot mag ook")
        assert not result.accepted
        assert not result.needs_ai_review

    def test_seller_with_website_rejected(self):
        result = filters.evaluate_listing(
            "iPhone 15 scherm kapot", "", CONFIG, seller_has_website=True
        )
        assert not result.accepted

    def test_priority_listing_rejected(self):
        result = filters.evaluate_listing(
            "iPhone 15 scherm kapot", "", CONFIG, priority_product="DAGTOPPER"
        )
        assert not result.accepted

    # --- 2026-07-22 junk-alert fixes ---------------------------------------
    # Every case below is a real listing that was auto-sent to Telegram via
    # the old "voor onderdelen"-family primary keywords (no AI review).

    def test_voor_onderdelen_routes_to_ai_not_auto_accept(self):
        # Was auto-accepted; must now go to AI so the actual defect is judged.
        result = evaluate("iPhone 15 voor onderdelen", "")
        assert not result.accepted
        assert result.needs_ai_review

    def test_voor_iemand_die_handig_is_routes_to_ai(self):
        result = evaluate("iPhone 16", "voor iemand die handig is")
        assert not result.accepted
        assert result.needs_ai_review

    def test_icloud_locked_for_parts_rejected(self):
        # "Iphone 17 pro met icloud" - locked, was auto-accepted via keyword.
        result = evaluate("Iphone 17 pro met icloud", "voor onderdelen")
        assert not result.accepted
        assert not result.needs_ai_review

    def test_icloud_vrij_still_allowed(self):
        # "icloud vrij" (iCloud-free) must NOT be caught by the lock excludes.
        result = evaluate("iPhone 15 voor onderdelen icloud vrij", "scherm kapot")
        assert result.accepted

    def test_corrosion_rejected(self):
        result = evaluate("Apple iPhone 15 Pro Max - Zilver - Corrosie", "voor onderdelen")
        assert not result.accepted
        assert not result.needs_ai_review

    def test_permanent_blocked_rejected(self):
        result = evaluate("iPhone 15 Pro Max 256 permanent geblokkeerd", "voor onderdelen")
        assert not result.accepted

    def test_wanted_ad_without_colon_rejected(self):
        # "Gezocht iphone 15 pro/ pro max beschadigd" - buyer, not a seller.
        result = evaluate("Gezocht iphone 15 pro pro max beschadigd", "beschadigd scherm")
        assert not result.accepted
        assert not result.needs_ai_review

    def test_wanted_ad_typo_rejected(self):
        result = evaluate("Gezoht iphone 16 pro beschadigd scherm", "beschadigd scherm")
        assert not result.accepted

    def test_bulk_lot_rejected(self):
        result = evaluate("Defecte iphones 21 stuks iphone 17 16 pro 15 pro", "voor onderdelen")
        assert not result.accepted
        assert not result.needs_ai_review

    def test_two_stuks_accessories_not_flagged_as_bulk(self):
        # Single-digit "2 stuks" (bundled cases) must NOT trip the bulk check.
        result = evaluate("iPhone 15 scherm kapot", "incl. 2 stuks hoesjes")
        assert result.accepted

    def test_i_phone_with_space_matches_target_model(self):
        # Real production miss 2026-07-15: m2420319890, "I phone 15 pro
        # 256 gb" - none of target_models' substrings match "I phone" with
        # a space, so a genuinely damaged phone titled this way would be
        # silently dropped.
        result = evaluate("I phone 15 pro 256 gb", "scherm kapot")
        assert result.accepted


# --- Hard-exclude negation blindness (2026-07-23 fix) -----------------------

class TestHardExcludeNegation:
    """
    Hard excludes were the last gate still doing naive substring matching:
    a seller ruling a defect OUT was rejected by the very words they used to
    rule it out. All six cases below were live-verified as rejected before
    the fix. Same class as the 2026-07-13 ambiguous-term negation miss.
    """

    def test_geen_waterschade_is_not_a_hard_exclude(self):
        # Must survive the hard-exclude gate. "gebarsten achterkant" isn't a
        # primary keyword, so the correct outcome is AI review, not a
        # silent reject on the word the seller used to rule water damage OUT.
        result = evaluate(
            "iPhone 16 gebarsten achterkant",
            "Geen corrosie, geen waterschade, puur cosmetisch.",
        )
        assert result.accepted or result.needs_ai_review, result.reason

    def test_icloud_vrij_is_not_a_lock(self):
        result = evaluate(
            "iPhone 15 Pro 256GB met barst in scherm",
            "Toestel is icloud vrij en geen waterschade, alleen scherm kapot.",
        )
        assert result.accepted, result.reason

    def test_icloud_verwijderd_is_not_a_lock(self):
        result = evaluate(
            "iPhone 15 Pro kapot scherm",
            "Komt met icloud verwijderd, gewoon te gebruiken.",
        )
        assert result.accepted, result.reason

    def test_icloud_account_eraf_is_not_a_lock(self):
        result = evaluate(
            "iPhone 15 Plus schade",
            "Verkocht met iCloud account eraf, scherm gebarsten.",
        )
        assert result.needs_ai_review or result.accepted, result.reason

    def test_niet_simlocked_is_not_a_simlock(self):
        result = evaluate(
            "iPhone 15 scherm kapot", "Niet simlocked, werkt met alle providers."
        )
        assert result.accepted, result.reason

    def test_real_waterschade_still_hard_excluded(self):
        # The fix must not blunt the gate: a genuine exclude still fires,
        # even alongside an unrelated negation.
        result = evaluate(
            "iPhone 15 kapot scherm",
            "Geen schade aan de achterkant, maar wel waterschade helaas.",
        )
        assert not result.accepted
        assert "waterschade" in result.reason

    def test_real_icloud_lock_still_hard_excluded(self):
        result = evaluate(
            "iPhone 16 Pro kapot scherm",
            "Let op: toestel is nog gekoppeld aan icloud van vorige eigenaar.",
        )
        assert not result.accepted
        assert "hard exclude" in result.reason


# --- Title normalization: Unicode + misspellings (2026-07-27 fix) -----------
#
# Every case below is a real title from seen_listings.db that was rejected
# as "not a target model" and never evaluated. Found during the 07-27 alert
# regression analysis, which showed 17 Pro / 17 Pro Max / 16 Pro Max alerts
# had dropped to exactly zero.

class TestTitleNormalization:
    def test_turkish_dotted_capital_i_is_folded(self):
        # U+0130 lowercases to "i" + COMBINING DOT ABOVE, which never
        # matched plain-ASCII "iphone". The same seller's listing was
        # re-seen daily 2026-07-14..07-23 and never once evaluated.
        assert "iphone" in filters.normalize_text("İPhone 15 Pro 128Gb")

    def test_dotted_capital_i_listing_reaches_the_pipeline(self):
        result = evaluate("İPhone 15 Pro 128Gb", "Scherm kapot, verder netjes.")
        assert result.accepted, result.reason

    @pytest.mark.parametrize(
        "typo",
        ["Ihpone", "iphon", "ipone", "iphoen", "iphne", "iphome", "ipohne"],
    )
    def test_common_misspellings_are_folded(self, typo):
        assert "iphone" in filters.normalize_text(f"{typo} 17 pro max")

    def test_ihpone_17_pro_max_is_a_target(self):
        # Real title, 2026-07-27 - the highest-value model in scope,
        # rejected outright on a transposed "h".
        result = evaluate("Ihpone 17 pro max", "Scherm gebarsten, verder goed.")
        assert result.accepted, result.reason

    def test_correct_spelling_is_left_alone(self):
        # The (?![a-z]) guard must stop "iphon" rewriting a valid "iphone".
        assert filters.normalize_text("iPhone 16 Pro") == "iphone 16 pro"

    def test_normalization_does_not_create_false_targets(self):
        # Folding must not turn a non-target into a target.
        result = evaluate("Ihpone 11 Pro", "Scherm kapot.")
        assert not result.accepted
        assert "not a target model" in result.reason


# --- AI response parsing (2026-07-27 fix) -----------------------------------

class TestAiResponseParsing:
    """The model occasionally appends prose after valid JSON. That raised
    "Extra data" and buried the listing as a reject. Real production miss
    2026-07-24: "iPhone 15 Pro 128GB Titanium Blauw - Oplaadpunt defect"."""

    def test_trailing_prose_after_json_still_parses(self):
        import ai_classifier
        raw = '{"relevant": true, "reason": "Cracked screen"}\n\nHope that helps!'
        parsed = ai_classifier._parse_first_json_object(raw)
        assert parsed["relevant"] is True
        assert parsed["reason"] == "Cracked screen"

    def test_leading_prose_before_json_still_parses(self):
        import ai_classifier
        raw = 'Here is my answer:\n{"relevant": false, "reason": "Accessory"}'
        parsed = ai_classifier._parse_first_json_object(raw)
        assert parsed["relevant"] is False

    def test_plain_json_is_unaffected(self):
        import ai_classifier
        parsed = ai_classifier._parse_first_json_object('{"relevant": true, "reason": "x"}')
        assert parsed["relevant"] is True

    def test_genuinely_unparseable_still_raises(self):
        import ai_classifier, json as _json
        with pytest.raises(_json.JSONDecodeError):
            ai_classifier._parse_first_json_object("I could not determine this.")

    def test_json_array_wrapper_yields_its_first_object(self):
        # Deliberately lenient: an array-wrapped verdict still contains a
        # usable answer, so we take the first object rather than throwing
        # the listing away. Fail-closed is reserved for output with no
        # parseable object at all (see the test above).
        import ai_classifier
        parsed = ai_classifier._parse_first_json_object(
            '[{"relevant": true, "reason": "Cracked back"}]'
        )
        assert parsed["relevant"] is True
