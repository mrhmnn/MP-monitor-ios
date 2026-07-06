"""
filters.py

All the "is this listing actually relevant" decision logic lives here,
kept separate from scraping and notification so it's easy to test and
tune independently (this is the part you'll iterate on the most).

Decision flow for a single listing (title + description combined as `text`):
  1. Does it mention a target model (14/15/16)? If not -> reject.
  2. Does it look like a business/shop listing, or a "wanted to buy" ad? If so -> reject.
  3. Does it contain a hard-exclude term (icloud lock, waterschade, etc)? If so -> reject.
  4. Does it contain a primary keyword (screen/back/charging damage)? If so -> ACCEPT.
  5. Does it contain an ambiguous term (e.g. "mankement") WITHOUT a negation nearby?
     If so -> route to AI classifier (handled by caller, not this module).
  6. Otherwise -> reject.
"""

from dataclasses import dataclass
import re
from typing import Optional


@dataclass
class FilterResult:
    accepted: bool
    reason: str
    needs_ai_review: bool = False


def _contains_any(text: str, phrases: list[str]) -> Optional[str]:
    """Return the first matching phrase found in text, or None."""
    for phrase in phrases:
        if phrase in text:
            return phrase
    return None


def _count_matches(text: str, phrases: list[str]) -> int:
    return sum(1 for phrase in phrases if phrase in text)


def matches_target_model(text: str, target_models: list[str]) -> bool:
    return _contains_any(text, target_models) is not None


def is_business_listing(text: str, indicators: list[str], threshold: int) -> bool:
    return _count_matches(text, indicators) >= threshold


def is_buyer_ad(text: str, indicators: list[str]) -> bool:
    return _contains_any(text, indicators) is not None


def has_hard_exclude(text: str, excludes: list[str]) -> Optional[str]:
    return _contains_any(text, excludes)


def has_primary_match(text: str, primary_keywords: list[str]) -> Optional[str]:
    return _contains_any(text, primary_keywords)


def has_unresolved_ambiguous_term(
    text: str, ambiguous_terms: list[str], negation_phrases: list[str]
) -> bool:
    """
    True if an ambiguous term (e.g. "mankement") appears WITHOUT a recognized
    negation phrase nearby. This is the cheap free pre-filter that stops us
    from sending "geen mankementen" listings to the AI classifier.
    """
    if not _contains_any(text, ambiguous_terms):
        return False
    if _contains_any(text, negation_phrases):
        return False
    return True


def evaluate_listing(title: str, description: str, config: dict) -> FilterResult:
    """
    Main entry point.

    IMPORTANT: model matching (14/15/16/17) is checked against the TITLE
    only, not the full description. Sellers reliably state what they're
    actually selling in the title; checking the description too caused
    false positives in production - e.g. an iPhone 12 listing whose
    description incidentally mentioned "I also have an iPhone 14 for sale"
    would wrongly pass the model filter. Damage/exclusion/noise checks
    still use the full combined text, since that detail often lives in
    the description.

    Same title-only rule applies to title_model_excludes (iPhone Air) -
    only the title determines model identity.
    """
    title_lower = title.lower()
    combined_text = f"{title} {description}".lower()

    if not matches_target_model(title_lower, config["target_models"]):
        return FilterResult(accepted=False, reason="not a target model (14-17) in title")

    # Explicitly reject iPhone Air listings even if the base-model check
    # passed via substring match. We use word-boundary regex here rather
    # than plain substring because Apple's naming is "iPhone 17 Air" (with
    # the generation number in between), so a simple "iphone air"
    # substring check would miss it. \b ensures we don't accidentally
    # match "airbag" or similar words.
    if re.search(r"\biphone\b.*\bair\b", title_lower):
        return FilterResult(accepted=False, reason="excluded model: iPhone Air variant")

    # Every target model (14-17) ships with OLED, not LCD - Apple has never
    # put an LCD panel on any of them. So "lcd" in the TITLE of one of
    # these listings isn't describing the phone's actual screen - it's
    # someone selling a cheap aftermarket replacement LCD part, or a
    # repair shop, not a damaged phone. Found in production: "iPhone 14
    # Pro Max scherm reparatie lcd" matched 'voor reparatie' from the
    # description while actually being a spare-part/repair listing.
    # Title-only check: descriptions of genuine damaged phones sometimes
    # mention "lcd" incidentally (e.g. quoting a repair quote they got),
    # which shouldn't disqualify them - only the title is a reliable
    # signal of what's actually being sold.
    if re.search(r"\blcd\b", title_lower):
        return FilterResult(
            accepted=False,
            reason="title mentions LCD - target models (14-17) are OLED-only, so this is a spare part/repair listing, not a phone",
        )

    if is_business_listing(
        combined_text,
        config["business_seller_indicators"],
        config["business_indicator_threshold"],
    ):
        return FilterResult(accepted=False, reason="looks like a business/shop listing")

    if is_buyer_ad(combined_text, config["buyer_ad_indicators"]):
        return FilterResult(accepted=False, reason="looks like a 'wanted to buy' ad, not a listing")

    excluded_term = has_hard_exclude(combined_text, config["hard_excludes"])
    if excluded_term:
        return FilterResult(accepted=False, reason=f"hard exclude matched: '{excluded_term}'")

    primary_term = has_primary_match(combined_text, config["primary_keywords"])
    if primary_term:
        vague_signal = _contains_any(title_lower, config.get("vague_title_signals", []))
        if vague_signal:
            return FilterResult(
                accepted=False,
                reason=f"vague title ('{vague_signal}') - needs AI confirmation despite keyword match",
                needs_ai_review=True,
            )
        return FilterResult(accepted=True, reason=f"primary keyword matched: '{primary_term}'")

    if has_unresolved_ambiguous_term(
        combined_text, config["ambiguous_terms"], config["negation_phrases"]
    ):
        return FilterResult(
            accepted=False,
            reason="ambiguous term present, needs AI review",
            needs_ai_review=True,
        )

    return FilterResult(accepted=False, reason="no relevant damage keywords found")
