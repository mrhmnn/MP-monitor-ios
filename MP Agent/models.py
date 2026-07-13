"""
models.py

iPhone model-name parsing, shared by main.py (market benchmark lookup)
and market.py (price-observation bucketing). Extracted from repair.py
when the Foneday repair-cost system was removed on 2026-07-12.
"""

import re
from typing import Optional

# Regex for the exact model variant. Order matters: "pro max" before "pro".
# The trailing (?![a-z0-9]) stops "iphone 16e"/"iphone 17e" from matching
# as a base 16/17 - the e-models aren't tracked.
# "iph" covers the common Marktplaats abbreviation ("IPH 14 Pro Max scherm
# kapot") - the filter pipeline already accepts those titles via
# target_models, but this parser silently dropped them from the market
# tracker and the [MARKT] alert line.
_MODEL_RE = re.compile(
    r"iph(?:one)?\s*(14|15|16|17)(?:\s*(pro\s*max|promax|pro|plus))?(?![a-z0-9])",
    re.IGNORECASE,
)


def parse_model(title: str) -> Optional[str]:
    """'iPhone 15 Pro Max 256GB kapot scherm' -> 'iphone 15 pro max'."""
    m = _MODEL_RE.search(title)
    if not m:
        return None
    generation, variant = m.group(1), (m.group(2) or "")
    variant = re.sub(r"\s+", " ", variant.lower()).replace("promax", "pro max").strip()
    return f"iphone {generation} {variant}".strip()
