"""
Unit tests for damage_detect.py - the standalone recall probe feeding the
DISAGREEMENT log line reviewed weekly.
"""

import damage_detect


class TestIsDamaged:
    def test_negated_damage_word_in_title_is_not_a_hit(self):
        damaged, terms = damage_detect.is_damaged("iPhone 15 geen barst")
        assert not damaged

    def test_negated_damage_word_in_description_is_not_a_hit(self):
        # 2026-07-15 fix: description matching had no negation-awareness at
        # all, unlike the title loop right above it. Real listings
        # m2420395281 ("Iphone 15 pro") and m2420389797 ("iPhone 16 128GB
        # Teal") both say "Geen schade" in the description and were firing
        # false DISAGREEMENT probe alerts despite filters.py correctly
        # rejecting both as damage-free.
        damaged, terms = damage_detect.is_damaged(
            "Iphone 15 pro",
            "Batterijconditie 87%. Geen schade - alleen enkele zeer lichte "
            "gebruikssporen. Werkt perfect, zonder mankementen.",
        )
        assert not damaged

    def test_bulk_lot_stuks_is_not_a_damage_hit(self):
        # 2026-08-09 probe review: the only two DISAGREEMENT lines in the
        # window were "stuk" matching inside "stuks" (= units, not broken) on
        # real wholesaler lots m2428747880 and m2428694201, both of which
        # filters.py had already rejected via the N-stuks bulk guard.
        for title in ("97 stuks iphone 16 / iphone 16e / iphone 16 plus",
                      "iphone 16 pro / iphone 16 pro max 256gb 83 stuks",
                      "iphone onderdelen 12 stukken"):
            damaged, terms = damage_detect.is_damaged(title)
            assert "stuk" not in terms, title

    def test_standalone_stuk_still_detected(self):
        damaged, terms = damage_detect.is_damaged("iPhone 15", "Scherm is stuk.")
        assert damaged
        assert "stuk" in terms

    def test_trap_occurrence_does_not_hide_a_later_real_hit(self):
        damaged, terms = damage_detect.is_damaged(
            "iPhone 15", "Verkoop per 2 stuks. Eentje is stuk gevallen."
        )
        assert "stuk" in terms

    def test_negated_occurrence_does_not_hide_a_later_real_hit(self):
        damaged, terms = damage_detect.is_damaged(
            "iPhone 15", "Geen schade aan de zijkant, wel schade op de achterkant."
        )
        assert "schade" in terms

    def test_real_damage_in_description_still_detected(self):
        damaged, terms = damage_detect.is_damaged("iPhone 15", "Scherm heeft een barst.")
        assert damaged
        assert "barst" in terms
