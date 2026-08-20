"""
filters.py

All the "is this listing actually relevant" decision logic lives here,
kept separate from scraping and notification so it's easy to test and
tune independently (this is the part you'll iterate on the most).

Decision flow for a single listing (title + description combined as `text`):
  1. Does it mention a target model (14/15/16)? If not -> reject.
  2. Does it look like a business/shop listing, or a "wanted to buy" ad? If so -> reject.
  3. Does it contain a hard-exclude term (icloud lock, waterschade, etc)? If so -> reject.
  4. Is it a bulk lot ("N stuks")? If so -> reject.
  5. Does it contain a primary keyword (screen/back/charging damage)? If so -> ACCEPT.
  6. Does it contain an ambiguous term (e.g. "mankement", "voor onderdelen")
     WITHOUT a negation nearby? If so -> route to AI classifier (handled by
     caller, not this module).
  7. Otherwise -> reject.

Note (2026-07-22): "voor onderdelen"/"voor reparatie"/"voor iemand die
handig is" are ambiguous terms (AI review), NOT primary keywords. A seller
flagging a phone "for parts" acknowledges damage but not its kind - often a
board-dead/locked/corroded brick, not a cheap screen swap.
"""

from dataclasses import dataclass
import re
import unicodedata
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


# Misspellings of "iphone" observed in real production titles between
# 2026-07-06 and 2026-07-27. Every one of these was rejected as "not a
# target model" without ever being evaluated - including "Ihpone 17 pro
# max" (the single highest-value model in scope) and "iphon 16 pro".
# Typo'd titles are a genuinely valuable niche: they don't surface in other
# buyers' searches either, so they attract less competition and sit longer.
# The trailing (?![a-z]) stops "iphon" from rewriting a correct "iphone",
# while still allowing digit-suffixed forms like "IPhoen13".
_IPHONE_TYPOS_RE = re.compile(
    r"\b(ihpone|iphon|ipone|iphoen|iphne|iphome|ipohne)(?![a-z])",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    """Lowercase and fold Unicode look-alikes + common misspellings of
    "iphone" into the canonical ASCII form the matchers expect.

    Unicode: the Turkish dotted capital I (U+0130) lowercases in Python to
    "i" + COMBINING DOT ABOVE (U+0307), which never matches the plain-ASCII
    "iphone" in target_models. Real production miss - the same seller's
    "Iphone 15 Pro" and "Iphone 16 Pro" listings were re-seen every day from
    2026-07-14 to 07-23 and not once evaluated, plus "Iphone 17 air"
    (2026-07-25). NFKD + dropping combining marks folds it back to "i".
    """
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # "i phone" (space) - real miss 2026-07-15, "I phone 14 pro 256 gb"
    text = re.sub(r"\bi\s+phone\b", "iphone", text)
    text = _IPHONE_TYPOS_RE.sub("iphone", text)
    return text


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
    r"\b(1[4-7])\b[^\d]{0,15}?(pro\s*max|pro|plus|max|\d{2,4}\s*gb|\d{2,4}gb)",
    re.IGNORECASE,
)

_GENERATION_RE = re.compile(r"\b(1[4-7])\b")


def enabled_generations(target_models: list[str]) -> set[str]:
    """The generation numbers actually switched on in config's target_models.

    Needed because target_models is the single place a generation gets
    enabled/disabled (the iPhone 14 was switched off 2026-07-23), but the
    bare-number fallback below matched 14-17 from a hardcoded regex range.
    Without this, disabling "iphone 14" in config still let a title like
    "14 Pro Max kapot scherm" through the fallback path - the config
    switch would have been half-effective in a way that's invisible until
    an unwanted alert shows up.
    """
    found = set()
    for entry in target_models:
        found.update(_GENERATION_RE.findall(entry.replace("iphone", " ").replace("iph", " ")))
    return found


def matches_target_model_fallback(
    title: str, description: str, target_models: Optional[list[str]] = None
) -> bool:
    """
    Fallback for bare-number titles (no "iPhone" in the title at all).
    Only trusts the bare-number regex if the description INDEPENDENTLY
    confirms "iphone" - that's what stops this from false-matching some
    unrelated "17 Pro" or "16 Plus" product that happens to share the
    category page, since the bare regex alone is deliberately loose.

    `target_models` gates which generations count; omitting it keeps the
    old "any of 14-17" behavior for callers that don't have the config.
    """
    match = _BARE_MODEL_RE.search(title)
    if not match:
        return False
    if target_models is not None and match.group(1) not in enabled_generations(target_models):
        return False
    return "iphone" in description.lower()


# Bulk wholesaler lots: "21 stuks iphone 17/16 pro / 15 pro etc" auto-accepted
# via the old "voor onderdelen" keyword (real, 2026-07-22). A private seller
# with one broken phone never writes "10 stuks"+; two-digit "N stuks" is a
# trader clearing inventory, not a single flip. Single-digit "2 stuks hoesjes"
# (accessories bundled with a real phone) deliberately does NOT trip this.
_BULK_LOT_RE = re.compile(r"\b\d{2,}\s*stuks\b", re.IGNORECASE)


def is_bulk_lot(text: str) -> bool:
    return _BULK_LOT_RE.search(text) is not None


# The other half of the lot vocabulary: Dutch "partij X" = "batch/lot of X",
# and lot sellers put it in the *title*. "N stuks" alone missed these because
# most partij lots never state a count ("Partij diverse iPhones"). They were
# passing only by accident - the target-model filter killed them, because lots
# usually list old models (5C/6S/3GS). That accident fails exactly when a lot
# does contain modern phones: "Partij iphones - iphone 14 pro - iphone 15 -
# iphone 15 pro" reached AI review, which judged the one iPhone 15's broken
# back cover on its own merits and alerted (real, 2026-08-13).
# Title-scoped on purpose: a real single-phone seller can mention a "partij"
# of accessories in the description, but never titles their ad that way. Of
# all 105 partij-titled listings seen since 2026-07-06, every one is a lot and
# none is a single target phone - so this costs zero recall.
_BULK_PARTIJ_RE = re.compile(r"\bpartij(en)?\b", re.IGNORECASE)


def is_bulk_partij(title: str) -> bool:
    return _BULK_PARTIJ_RE.search(title) is not None


# Accessory listings: a case, a screen protector, a charging cable. Not phones,
# so never an AI judgment call - "is this a phone" belongs in the filter, the
# same place "is this a target model" already lives (2026-08-20). Before this,
# ~8 accessory listings a day reached AI review, and once the AI stopped
# gating they would have alerted.
#
# Rule 1 (position): the accessory word must BE the product - either the title
# opens with it ("Screenprotector iPhone 11 pro"), or the accessory is offered
# *voor* a phone ("Wave Hoesje voor iPhone 12 Pro Max").
_ACCESSORY_NOUN = (
    r"(?:hoesje|hoesjes|hoes|hoezen|telefoonhoes|telefoonhoesje|telefoonhoesjes"
    r"|case|cases|cover|covers"
    r"|screenprotector|screen ?protector|beschermglas|tempered ?glass|privacy ?glas"
    r"|oplader|oplaadkabel|adapter|earpods|airpods|oordopjes|bumper)"
)

_ACCESSORY_RE = re.compile(
    rf"^\W*(?:\w+\s+){{0,1}}{_ACCESSORY_NOUN}\b"
    rf"|\b{_ACCESSORY_NOUN}\b[^,]{{0,40}}?\bvoor\b",
    re.IGNORECASE,
)

# Rule 2 (brand-first cases, added 2026-08-20 #2): position alone let a whole
# class of case listings through, because sellers put the brand and the phone
# model in front of the accessory word and never write "voor" - four of them
# alerted on 08-20 ("Rode iPhone 15 Plus Silicone Case met MagSafe", "OtterBox
# Lumen Series Apple iPhone 15 Pro Clear Case", "Nieuwe Pitaka Edge Case voor
# iPhone 16 Pro", "iPhone 15 Pro Max Clear Case met MagSafe"), plus the
# "Bluebolt iPhone 17 metalen hoes" the position rule was documented as
# missing. What they all share is a case-TYPE word glued to the noun, which a
# phone listing has no reason to contain.
_ACCESSORY_QUALIFIER = (
    r"(?:clear|silicone|siliconen|leather|leren|kunstleer|magsafe|book|wallet"
    r"|portemonnee|pasjes|flip|flipcase|folio|tpu|hardcase|hard|soft|metalen"
    r"|metal|transparant|transparante|doorzichtig|edge|shockproof|waterproof"
    r"|waterdicht|waterdichte|armor|rugged|glitter|tough|fashion)"
)
_ACCESSORY_PAIR_RE = re.compile(
    rf"\b{_ACCESSORY_QUALIFIER}[\s-]*{_ACCESSORY_NOUN}\b", re.IGNORECASE
)

# ...unless the case is being thrown IN with a phone. "iPhone 13 128GB Groen -
# Inc Magsafe hoesje", "iPhone XS Max - Werkt goed, inclusief Apple leren
# hoesje": an inclusion word right before the pair means the phone is the
# product and the case is a freebie. Without this guard rule 2 blocks real
# damaged phones, which is the one thing it must never do.
_ACCESSORY_INCLUDED_RE = re.compile(
    r"\b(incl|inclusief|inclusive|inc|met|gratis|erbij|bijgeleverd|inbegrepen)"
    r"\b[\w\s.-]{0,12}$"
    r"|\+\s*[\w\s.-]{0,12}$",
    re.IGNORECASE,
)

# NOT used: Marktplaats' own "mobile_phones_cases_and_covers_apple_iphone"
# category. It looks like the perfect free signal and it is a trap - checked
# live 2026-08-20, EIGHT of the listings in it across five queries were real
# phones, including "Iphone 15" with a gebroken achterkant and an "Iphone 15
# roze" whose back is kapot. Sellers pick the category carelessly, exactly
# like the July 2026 iPhone that sat under "Telefoon-opladers". Title text is
# the only accessory signal that holds.


def is_accessory_listing(title: str) -> bool:
    """True if the listing sells an accessory rather than a phone.

    Validated against all 8826 listings in DB history: of the 32 titles the
    brand-first rule newly blocks, every single one is a genuine accessory,
    and no listing that ever alerted as a real phone is caught.
    Known miss, deliberately: a case titled with no type word and no "voor"
    at all. Letting one case through beats blocking one real phone.
    """
    if _ACCESSORY_RE.search(title):
        return True
    return any(
        not _ACCESSORY_INCLUDED_RE.search(title[: match.start()])
        for match in _ACCESSORY_PAIR_RE.finditer(title)
    )


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
    seller_is_verified: bool = False,
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
    # normalize_text() folds case, Unicode look-alikes (Turkish dotted I) and
    # the "i phone"/"ihpone"/"iphon" spelling variants into canonical ASCII -
    # see its docstring for the production misses behind each rule. Applied to
    # the combined text too so a typo in the description can't hide damage
    # keywords from the gates below.
    title_lower = normalize_text(title)
    combined_text = normalize_text(f"{title} {description}")

    if not matches_target_model(title_lower, config["target_models"]):
        if not matches_target_model_fallback(
            title_lower, description, config["target_models"]
        ):
            return FilterResult(accepted=False, reason="not a target model in title")

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
    # sellerInformation.isVerified - Marktplaats' own verified/business badge
    # (2026-08-20, Milad: "any seller which is verified/a business"). Measured
    # live the same day across 346 listings from the damage queries: 11 were
    # verified, and every one was a trader - the "Used Products" chain, "Best
    # Buy Phone", "Smartphones Emmeloord", a bulk "partij" seller and two
    # wanted-ads. Not one was a private seller with a broken phone. Stronger
    # than showWebsiteUrl, which all 11 had set to false.
    if config.get("reject_verified_sellers", True) and seller_is_verified:
        return FilterResult(
            accepted=False,
            reason="verified/business seller (Marktplaats badge) - a shop, not a private seller",
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

    if is_bulk_lot(combined_text):
        return FilterResult(
            accepted=False,
            reason="bulk lot (N stuks) - wholesaler clearing inventory, not a single flip",
        )

    if is_bulk_partij(title):
        return FilterResult(
            accepted=False,
            reason="bulk lot ('partij' in title) - wholesaler lot, not a single flip",
        )

    if is_accessory_listing(title):
        return FilterResult(
            accepted=False,
            reason="accessory listing (case/protector/cable), not a phone",
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

    # Negation-stripped FIRST (2026-07-23). Hard excludes were the last gate
    # still doing naive substring matching, so a seller ruling a defect OUT
    # was rejected by the very words they used to rule it out: "geen
    # waterschade" matched 'waterschade', "icloud vrij" matched 'met icloud',
    # "niet simlocked" matched 'simlocked'. Same negation-blindness class as
    # the 2026-07-13 ambiguous-term miss, in the one place it was never
    # fixed - and the 2026-07-22 lock/corrosion additions made it bite hard.
    # A genuine exclude still fires: "geen schade aan scherm, wel
    # waterschade" keeps 'waterschade' after the negation is stripped.
    excluded_term = has_hard_exclude(
        strip_negation_phrases(combined_text, config["negation_phrases"]),
        config["hard_excludes"],
    )
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
