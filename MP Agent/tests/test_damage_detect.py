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

    def test_real_damage_in_description_still_detected(self):
        damaged, terms = damage_detect.is_damaged("iPhone 15", "Scherm heeft een barst.")
        assert damaged
        assert "barst" in terms
