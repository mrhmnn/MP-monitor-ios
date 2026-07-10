"""
repair.py

Best-effort [FONEDAY] repair-cost estimate for a matched listing, shown
in the Telegram alert as bidding context.

(This module replaced profit.py on 2026-07-10: the Swappie trade-in
payout + profit/break-even lines were removed because they added 2-3
lines of noise to every alert without ever being filtered on. Git
history has the full profit system if it's ever wanted back.)

Data source: data/parts_prices.yaml - [FONEDAY] wholesale repair-part
cost per model/category, a committed file refreshed by its own script
(refresh_prices.py), NOT fetched live during a scan - a scan must never
fail or slow down because a pricing site is unreachable.

Everything here is best-effort: a listing with an unparseable model
still gets notified - it just carries no repair number. Never raise out
of estimate_repair(); a pricing bug must not kill a scan.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# --- Damage-category inference ----------------------------------------------
# Maps Dutch damage vocabulary (the same vocabulary config.yaml's keyword
# lists are built from) to a Foneday parts category. Scanned against the
# listing's combined title+description text; every category that matches
# contributes its part price to the repair estimate.
DAMAGE_SIGNALS = {
    "screen": ["scherm", "display", "touchscreen", "touch screen",
               "groene streep", "streep in", "glas gebarsten", "glas kapot"],
    "back_cover": ["achterkant", "achterglas", "backcover", "back cover",
                   "glas achterkant", "deksel"],
    "charging_port": ["laadt niet", "laad niet", "oplaadprobleem", "laadprobleem",
                      "laadpoort", "oplaadpoort", "lightning poort", "usb-c poort",
                      "gaat niet opladen", "oplaadt niet"],
    "battery": ["batterij", "accu "],
    "camera_lens": ["camera glaasje", "camera glas", "cameraglas", "camera lens",
                    "cameralens"],
}

# Regex for the exact model variant. Order matters: "pro max" before "pro".
# The trailing (?![a-z0-9]) stops "iphone 16e"/"iphone 17e" from matching
# as a base 16/17 - the e-models have different prices and aren't covered.
_MODEL_RE = re.compile(
    r"iphone\s*(14|15|16|17)(?:\s*(pro\s*max|promax|pro|plus))?(?![a-z0-9])",
    re.IGNORECASE,
)


@dataclass
class RepairEstimate:
    model: Optional[str] = None            # "iphone 15 pro max" (lowercase key)
    repair_cost: Optional[float] = None    # [FONEDAY] parts total
    repair_parts: list[str] = field(default_factory=list)
    repair_assumed: bool = False           # True if damage type defaulted to screen


_parts_cache: Optional[dict] = None


def _load_parts() -> dict:
    global _parts_cache
    if _parts_cache is None:
        try:
            path = DATA_DIR / "parts_prices.yaml"
            _parts_cache = yaml.safe_load(path.read_text(encoding="utf-8")).get(
                "repair_costs", {}
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not load parts_prices.yaml: %s", exc)
            _parts_cache = {}
    return _parts_cache


def parse_model(title: str) -> Optional[str]:
    """'iPhone 15 Pro Max 256GB kapot scherm' -> 'iphone 15 pro max'."""
    m = _MODEL_RE.search(title)
    if not m:
        return None
    generation, variant = m.group(1), (m.group(2) or "")
    variant = re.sub(r"\s+", " ", variant.lower()).replace("promax", "pro max").strip()
    return f"iphone {generation} {variant}".strip()


def infer_repair_categories(text: str) -> list[str]:
    text_lower = text.lower()
    return [cat for cat, signals in DAMAGE_SIGNALS.items()
            if any(s in text_lower for s in signals)]


def estimate_repair(title: str, combined_text: str, config: dict) -> RepairEstimate:
    """Best-effort repair-cost estimate for one matched listing. Never raises."""
    try:
        return _estimate(title, combined_text, config)
    except Exception as exc:  # noqa: BLE001
        logger.error("Repair estimate failed for '%s': %s", title, exc)
        return RepairEstimate()


def _estimate(title: str, combined_text: str, config: dict) -> RepairEstimate:
    est = RepairEstimate()
    est.model = parse_model(title)
    if est.model is None:
        return est

    parts_model = _load_parts().get(est.model, {})
    if parts_model:
        categories = infer_repair_categories(combined_text)
        if not categories:
            # Matched listing with no recognizable damage vocabulary
            # (usually an AI-accepted "mankement" case). Screens are by far
            # the most common damage in our queries - assume one, flagged
            # so the alert says it's an assumption.
            categories = ["screen"]
            est.repair_assumed = True
        screen_tier = config.get("screen_repair_tier", "screen_oled")
        total = 0.0
        for cat in categories:
            key = screen_tier if cat == "screen" else cat
            price = parts_model.get(key)
            if price is None:
                continue
            total += price
            est.repair_parts.append("screen" if cat == "screen" else cat)
        if est.repair_parts:
            est.repair_cost = round(total, 2)

    return est
