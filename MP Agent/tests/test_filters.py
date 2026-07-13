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
        result = evaluate("iPhone 14", "Glas kapot aan achterzijde, verder geen problemen.")
        assert result.needs_ai_review

    def test_unmatched_bucket_pure_negation_skips_ai(self):
        result = evaluate("iPhone 14 als nieuw", "Geen problemen, altijd hoesje om gehad.")
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
        result = evaluate("iPhone 14 Pro - lees beschrijving!", "scherm kapot")
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
        result = evaluate("iPhone 14 Pro Max scherm reparatie lcd", "voor reparatie")
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
