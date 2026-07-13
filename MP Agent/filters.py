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

# Only used to decide whether a negation-guarded unmatched listing still
# deserves an AI look (its DAMAGE list answers "is damage mentioned at
# all?"). Safe here because hard_excludes have already run by the time
# it's consulted - see damage_detect.py's own docstring.
import damage_detect


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


# Catches titles that name a generation number + phone-spec qualifier
# (Pro/Plus/Max/storage size) without ever writing the word "iPhone" -
# e.g. "3x 17 Pro en 4x 17 256 gb met kapotte displays" (real production
# miss: seller assumed the Apple iPhone category made it obvious). Requires
# a qualifier alongside the bare number so it doesn't fire on any random
# "14"-"17" digit; the description check in matches_target_model_fallback
# is what actually keeps this safe from false-matching unrelated listings.
_BARE_MODEL_RE = re.compile(
    r"\b1[4-7]\b[^\d]{0,15}?(pro\s*max|pro|plus|max|\d{2,4}\s*gb|\d{2,4}gb)",
    re.IGNORECASE,
)


def matches_target_model_fallback(title: str, description: str) -> bool:
    """
    Fallback for bare-number titles (no "iPhone" in the title at all).
    Only trusts the bare-number regex if the description INDEPENDENTLY
    confirms "iphone" - that's what stops this from false-matching some
    unrelated "17 Pro" or "16 Plus" product that happens to share the
    category page, since the bare regex alone is deliberately loose.
    """
    if not _BARE_MODEL_RE.search(title):
        return False
    return "iphone" in description.lower()


def is_business_listing(text: str, indicators: list[str], threshold: int) -> bool:
    return _count_matches(text, indicators) >= threshold


def is_buyer_ad(text: str, indicators: list[str]) -> bool:
    return _contains_any(text, indicators) is not None


def has_hard_exclude(text: str, excludes: list[str]) -> Optional[str]:
    return _contains_any(text, excludes)


def has_primary_match(text: str, primary_keywords: list[str]) -> Optional[str]:
    return _contains_any(text, primary_keywords)


def strip_negation_phrases(text: str, negation_phrases: list[str]) -> str:
    """Remove every negation-phrase occurrence from the text, so that only
    NON-negated damage mentions remain for the checks that follow."""
    for phrase in negation_phrases:
        if phrase in text:
            text = text.replace(phrase, " ")
    return text


def has_unresolved_ambiguous_term(
    text: str, ambiguous_terms: list[str], negation_phrases: list[str]
) -> bool:
    """
    True if an ambiguous term (e.g. "mankement") appears outside of any
    negation phrase. This is the cheap free pre-filter that stops us from
    sending "geen mankementen" listings to the AI classifier.

    The negation phrases are stripped from the text FIRST, and the
    ambiguous terms are checked against what's left. A global "any negation
    present -> skip" check (the old behavior) rejected real damage listings:
    sellers routinely write "achterkant schade, verder geen problemen" -
    the "geen problemen" is about everything EXCEPT the damage they just
    described (real production miss, 2026-07-13).
    """
    remaining = strip_negation_phrases(text, negation_phrases)
    return _contains_any(remaining, ambiguous_terms) is not None


def evaluate_listing(
    title: str,
    description: str,
    config: dict,
    seller_has_website: bool = False,
    priority_product: str = "NONE",
) -> FilterResult:
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

    Exception: matches_target_model_fallback() - if the title has zero
    "iphone" substring but names a bare generation number with a phone-spec
    qualifier (e.g. "17 Pro", "17 256 gb"), the description is checked for
    an independent "iphone" confirmation. Found in production: a bulk-lot
    listing titled "3x 17 Pro en 4x 17 256 gb met kapotte displays" never
    said "iPhone" anywhere in the title, relying on the Apple iPhone
    category to make it obvious - matches_target_model() alone silently
    dropped it every scan.

    `seller_has_website` / `priority_product` come from Marktplaats' own
    structured listing data (scraper.Listing) - much stronger business
    signals than keyword heuristics, but only available via the JSON
    extraction path, so the keyword-based business check stays as backup.
    """
    title_lower = title.lower()
    combined_text = f"{title} {description}".lower()

    if not matches_target_model(title_lower, config["target_models"]):
        if not matches_target_model_fallback(title_lower, description):
            return FilterResult(accepted=False, reason="not a target model (14-17) in title")

    # Structured business-seller signals from Marktplaats' own data:
    # a seller with a linked business website is a shop by definition, and
    # paid promoted placements (DAGTOPPER etc.) are overwhelmingly repair
    # shops - private individuals selling one broken phone don't pay to
    # promote it. Both are config-gated so they're easy to loosen if a
    # legitimate listing ever gets caught.
    if config.get("reject_seller_with_website", True) and seller_has_website:
        return FilterResult(accepted=False, reason="seller has a business website linked")
    if config.get("reject_priority_listings", True) and priority_product not in ("", "NONE"):
        return FilterResult(
            accepted=False,
            reason=f"paid promoted listing ({priority_product}) - almost always a shop",
        )

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

    # TITLE-only on purpose: buyer ads ("gezocht: kapotte iphone") state
    # their intent in the title, while genuine sellers regularly use the
    # same phrases incidentally in descriptions ("ik zoek een snelle
    # verkoop") - checking the combined text was rejecting real listings.
    if is_buyer_ad(title_lower, config["buyer_ad_indicators"]):
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

    # No keyword matched at all - but this listing came from a damage-focused
    # search query, names a target model, and survived every shop/buyer/
    # hard-exclude gate. "iPhone 14 kapot - zie fotos" describes exactly the
    # phones we want and contains zero primary keywords; silently dropping
    # this bucket loses real deals over phrasing. Let the AI judge them.
    # Negation-guarded so "geen schade" mint listings never cost a call -
    # but the guard only holds if NO broad damage word survives stripping
    # the negation phrases: "kapot glas, verder geen problemen" describes
    # real damage and must still reach the AI (same miss class as the
    # ambiguous-term path). Config-gated in case call volume needs reining in.
    if config.get("ai_review_unmatched", True):
        remaining = strip_negation_phrases(combined_text, config["negation_phrases"])
        no_negation = remaining == combined_text
        if no_negation or _contains_any(remaining, damage_detect.DAMAGE):
            return FilterResult(
                accepted=False,
                reason="no keyword match - needs AI judgment",
                needs_ai_review=True,
            )

    return FilterResult(accepted=False, reason="no relevant damage keywords found")
