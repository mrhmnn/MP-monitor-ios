"""
models.py

iPhone model-name parsing, shared by main.py (market benchmark lookup)
and market.py (price-observation bucketing). Extracted from repair.py
when the Foneday repair-cost system was removed on 2026-07-12.
"""

import re
from typing import Optional

import filters

# Regex for the exact model variant. Order matters: "pro max" before "pro",
# and "max pro" is in there because sellers write the words in that order
# surprisingly often ("Iphone 15 max pro 256gb") - 11 rows in live history.
#
# The 18 was added 2026-09-17; it ships Pro-only for now, but the generation
# is listed bare so the base 18 and 18e parse correctly the moment they
# launch (spring 2027). "iph" covers the common Marktplaats abbreviation
# ("IPH 14 Pro Max scherm kapot") - the filter pipeline already accepts those
# titles via target_models, but this parser silently dropped them from the
# market tracker and the [MARKT] alert line.
#
# WIDENED 2026-10-03 (audit). The variant used to have to sit IMMEDIATELY
# after the generation number, so three real title shapes collapsed to the
# base model or to None - 178 market rows and 15 alerts, measured:
#   "Iphone 15 Max Pro 256GB Grey (achterkant barst)"  -> iphone 15
#   "IPHONE 17/256 PRO MAX NIEUW"                      -> iphone 17
#   "iPhone15Pro128GB Gebarsten achterkant"            -> None
#   "iPhone 15met 128GB, gebarsten achterkant"         -> None
# A 15 Pro priced against the base-15 reference is EUR350 instead of EUR450
# of resale, which is enough to move the deal verdict a whole tier; None
# loses the deal line and the [MARKT] line outright. That last listing
# alerted four times (09-25, 09-26, 09-28, 10-02) with no price verdict.
#
# Three guards keep the widening safe, and each one earned its place against
# the live corpus:
#   (?!\d)          - "iPhone 150-" is not an iPhone 15.
#   (?!e(?![a-z]))  - "iphone 16e"/"17e" must NOT read as a base 16/17; the
#                     e-models aren't tracked. This replaces the old trailing
#                     (?![a-z0-9]), which also blocked the glued "15Pro128GB".
#   [^a-z]{0,10}    - the gap may skip storage digits and separators
#                     ("17/256 PRO") but NEVER a letter, so it cannot jump
#                     over a word into a neighbouring model name. This is
#                     what keeps "iPhone 15 problemen met scherm" from
#                     parsing as a 15 Pro, and why "Iphone 18 zwart pro" is
#                     deliberately left as a base 18.
#   (?![a-z])       - after the variant, so "pro" cannot match inside
#                     "problemen" and "plus" cannot match inside "plusminus".
_MODEL_RE = re.compile(
    r"iph(?:one)?\s*(1[4-8])(?!\d)(?!e(?![a-z]))"
    r"(?:([^a-z]{0,10})(pro\s*max|promax|max\s*pro|pro|plus)(?![a-z]))?",
    re.IGNORECASE,
)

# A gap containing another iPhone generation means the variant belongs to a
# DIFFERENT phone in a list, not to ours: "iPhone 17 / 17 Pro / 17 Pro Max"
# and "Kapotte Iphone 15 &16 pro" would otherwise bucket as 17 Pro and 15
# Pro. Dropping the variant in that case returns the base model, which is
# the honest answer for a multi-model title. Checking 10-19 rather than just
# 14-18 so an older generation in the list counts too ("iPhone 14/13/13
# Pro"); storage numbers (128/256/512) can't collide with it.
_OTHER_GENERATION_IN_GAP = re.compile(r"\b1[0-9]\b")


def parse_model(title: str) -> Optional[str]:
    """'iPhone 15 Pro Max 256GB kapot scherm' -> 'iphone 15 pro max'.

    Runs the title through filters.normalize_text first (2026-07-28) so the
    Unicode and misspelling folding added for the match pipeline applies here
    too. Without it, "Ihpone 17 pro max" cleared the filters but parsed as
    None - costing it the [MARKT] line, the deal score, and the high-value
    accept policy. That listing was one of the real 07-27 misses.
    """
    m = _MODEL_RE.search(filters.normalize_text(title))
    if not m:
        return None
    generation, gap, variant = m.group(1), (m.group(2) or ""), (m.group(3) or "")
    if _OTHER_GENERATION_IN_GAP.search(gap):
        variant = ""
    variant = (
        re.sub(r"\s+", " ", variant.lower())
        .replace("promax", "pro max")
        .replace("max pro", "pro max")
        .strip()
    )
    return f"iphone {generation} {variant}".strip()
