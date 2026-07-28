"""
Tests for the high-value model recall policy (2026-07-28).

Why this exists: iPhone 17 Pro, 17 Pro Max and 16 Pro Max produced EXACTLY
ZERO alerts between 07-24 and 07-27, against roughly 12 expected. Nothing
was broken - the cheap-repair-only prompt simply rejects most of what gets
listed on a €650-1050 phone (Face ID, camera module, bare "voor onderdelen").
Milad's call: "I'd rather have 3 listings, 2 wrong 1 right, than 0."

These tests pin the wiring, not Haiku's judgment: that the suffix is applied
to exactly the right models, that it carries the categories it is supposed
to reverse, and that it doesn't quietly undo the rules he asked for.
"""

import yaml
from pathlib import Path

import ai_classifier
import models

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
HIGH_VALUE = set(CONFIG.get("high_value_models", []))


def _is_high_value(title: str) -> bool:
    """Mirrors the check in main.py's AI-review branch."""
    return models.parse_model(title) in HIGH_VALUE


# --- which listings get the widened policy --------------------------------

def test_covered_models_are_the_pro_tier():
    assert HIGH_VALUE == {
        "iphone 15 pro", "iphone 16 pro", "iphone 16 pro max",
        "iphone 17 pro", "iphone 17 pro max",
    }


def test_high_value_titles_are_recognised():
    for title in [
        "iPhone 17 Pro Max - Defect",
        "Iphone 17 pro 256 gb",
        "iPhone 16 Pro Max 256GB Natural Titanium",
        # Where the expensive-repair rejections actually concentrate: 24 on
        # the 15 Pro alone, vs 3 across all three zero-alert models.
        "iPhone 15 Pro 128GB Titanium Blauw - Oplaadpunt defect",
        "ZGAN Apple Iphone 15 PRO - Face ID defect",
        "iPhone 16 Pro voor onderdelen",
    ]:
        assert _is_high_value(title), title


def test_non_pro_models_keep_the_strict_policy():
    """The widened rules must not leak onto models where an expensive repair
    really does eat the whole margin - base 15/16/17 resell €370-€500."""
    for title in [
        "iPhone 15 128 GB - Gebruikt met schade",
        "iPhone 17 128GB",
        "iPhone 16 Plus 256GB",
        "iPhone 15 Pro Max 256GB",
    ]:
        assert not _is_high_value(title), title


def test_typo_titles_still_reach_the_high_value_policy():
    """The real 07-27 miss. PR #9 got this title through the filters, but
    parse_model returned None, so it would have been treated as an unknown
    model and got the strict prompt anyway."""
    assert _is_high_value("Ihpone 17 pro max: prijs staat vast.")
    assert _is_high_value("İphone 17 Pro Max 256GB")


# --- what the widened policy actually says --------------------------------

def test_suffix_reverses_the_expensive_repair_rejections():
    """These are the exact rejection reasons seen in production on the three
    zeroed models."""
    suffix = ai_classifier.HIGH_VALUE_SUFFIX.lower()
    assert "face id" in suffix
    assert "camera module" in suffix
    assert "voor onderdelen" in suffix


def test_suffix_keeps_the_genuine_write_offs_excluded():
    suffix = ai_classifier.HIGH_VALUE_SUFFIX.lower()
    for write_off in ["water damage", "motherboard", "icloud lock", "replica"]:
        assert write_off in suffix, write_off


def test_suffix_does_not_undo_the_scratch_rule():
    """Milad's own 07-24 request after near-mint 15 Pros kept alerting.
    Trading precision for recall on high-value models must not silently
    reopen a rule he specifically asked for."""
    assert "surface-scratch rule above still applies" in ai_classifier.HIGH_VALUE_SUFFIX


def test_strict_prompt_is_unchanged_for_normal_listings():
    """Nothing is appended unless the model is high-value - the base prompt
    is what 15/16-gen listings still get judged by."""
    assert ai_classifier.HIGH_VALUE_SUFFIX not in ai_classifier.SYSTEM_PROMPT
    assert ai_classifier.SYSTEM_PROMPT.rstrip().endswith(
        '{"relevant": true or false, "reason": "one short sentence in English"}'
    )


def test_classifier_accepts_the_high_value_flag():
    """Signature guard: main.py passes high_value= by keyword."""
    import inspect

    params = inspect.signature(ai_classifier.classify_ambiguous_listing).parameters
    assert "high_value" in params
    assert params["high_value"].default is False
