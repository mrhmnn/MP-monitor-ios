"""
profit.py

Phase 2 profit estimate for a matched listing:

    profit = [SWAPPIE] resale price - asking price - [FONEDAY] repair cost

Data sources (both committed files under data/, refreshed by their own
scripts, NOT fetched live during a scan - a scan must never fail or slow
down because a pricing site is unreachable):

  data/swappie_prices.json   [SWAPPIE] retail price per model/storage/grade
                             (refresh_swappie_prices.py). Grade C = "Heel
                             goed" (Good), grade D = "Redelijk" (Fair) -
                             the realistic resale range for a repaired flip.
  data/parts_prices.yaml     [FONEDAY] wholesale repair-part cost per
                             model/category (refresh_prices.py).

Everything here is best-effort: a listing with an unparseable model,
storage, or price still gets notified - it just carries fewer numbers.
Never raise out of estimate_profit(); a pricing bug must not kill a scan.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# --- Swappie grade semantics -------------------------------------------------
# Swappie NL sells refurbished phones in four grades:
#   A = Premium (als nieuw)   B = Uitstekend   C = Heel goed   D = Redelijk
# A phone we repaired with aftermarket parts realistically competes with
# Swappie's C ("Good") at best, and D ("Fair") is the conservative floor.
# If the exact grade is out of stock at Swappie, fall back to the nearest
# grade so a sparse model (e.g. a just-released generation) still gets an
# estimate - the grade actually used is reported in the result.
GRADE_GOOD_FALLBACK = ["C", "B", "D", "A"]
GRADE_FAIR_FALLBACK = ["D", "C", "B", "A"]
GRADE_LABELS = {"A": "Premium", "B": "Uitstekend", "C": "Heel goed", "D": "Redelijk"}

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

_STORAGE_RE = re.compile(r"\b(1\s?tb|64|128|256|512)\s*(?:gb|g\b)?", re.IGNORECASE)

_PRICE_RE = re.compile(r"€\s*([\d.]+(?:,\d{1,2})?)")


@dataclass
class ProfitEstimate:
    model: Optional[str] = None            # "iphone 15 pro max" (lowercase key)
    storage_gb: Optional[int] = None       # storage used for the lookup
    storage_assumed: bool = False          # True if title didn't state storage
    swappie_good: Optional[float] = None   # [SWAPPIE] resale, Good condition
    swappie_good_grade: str = ""           # grade actually used (normally C)
    swappie_fair: Optional[float] = None   # [SWAPPIE] resale, Fair condition
    swappie_fair_grade: str = ""           # grade actually used (normally D)
    repair_cost: Optional[float] = None    # [FONEDAY] parts total
    repair_parts: list[str] = field(default_factory=list)
    repair_assumed: bool = False           # True if damage type defaulted to screen
    asking_price: Optional[float] = None   # parsed from the listing
    asking_is_bid_floor: bool = False      # "Bieden vanaf" - real price will be higher
    profit_good: Optional[float] = None    # swappie_good - asking - repair
    profit_fair: Optional[float] = None    # swappie_fair - asking - repair
    break_even: Optional[float] = None     # swappie_fair - repair = max sane buy price


_swappie_cache: Optional[dict] = None
_parts_cache: Optional[dict] = None


def _load_swappie() -> dict:
    global _swappie_cache
    if _swappie_cache is None:
        try:
            path = DATA_DIR / "swappie_prices.json"
            _swappie_cache = json.loads(path.read_text(encoding="utf-8")).get("models", {})
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not load swappie_prices.json: %s", exc)
            _swappie_cache = {}
    return _swappie_cache


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


def parse_storage(text: str) -> Optional[int]:
    m = _STORAGE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).lower().replace(" ", "")
    return 1024 if raw == "1tb" else int(raw)


def parse_asking_price(price_text: str) -> tuple[Optional[float], bool]:
    """'€250' -> (250.0, False); 'Bieden vanaf €50' -> (50.0, True);
    'Bieden' -> (None, False). Handles '1.250' and '250,50' formats."""
    if not price_text:
        return None, False
    m = _PRICE_RE.search(price_text)
    if not m:
        return None, False
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw), "vanaf" in price_text.lower()
    except ValueError:
        return None, False


def infer_repair_categories(text: str) -> list[str]:
    text_lower = text.lower()
    return [cat for cat, signals in DAMAGE_SIGNALS.items()
            if any(s in text_lower for s in signals)]


def _pick_grade(prices_by_grade: dict, order: list[str]) -> tuple[Optional[float], str]:
    for grade in order:
        if grade in prices_by_grade:
            return prices_by_grade[grade], grade
    return None, ""


def estimate_profit(title: str, combined_text: str, price_text: str,
                    config: dict) -> ProfitEstimate:
    """Best-effort profit estimate for one matched listing. Never raises."""
    try:
        return _estimate(title, combined_text, price_text, config)
    except Exception as exc:  # noqa: BLE001
        logger.error("Profit estimate failed for '%s': %s", title, exc)
        return ProfitEstimate()


def _estimate(title: str, combined_text: str, price_text: str,
              config: dict) -> ProfitEstimate:
    est = ProfitEstimate()
    est.model = parse_model(title)
    if est.model is None:
        return est

    est.asking_price, est.asking_is_bid_floor = parse_asking_price(price_text)

    # --- [SWAPPIE] resale lookup ---
    swappie_model = _load_swappie().get(est.model, {})
    if swappie_model:
        storage = parse_storage(title) or parse_storage(combined_text)
        if storage is None or str(storage) not in swappie_model:
            # No storage stated (or a size Swappie doesn't stock for this
            # model): assume the SMALLEST tier Swappie sells - damaged
            # Marktplaats phones skew base-storage, and assuming small
            # keeps the resale estimate conservative.
            est.storage_assumed = True
            storage = min(int(s) for s in swappie_model)
        est.storage_gb = storage
        grades = swappie_model[str(storage)]
        est.swappie_good, est.swappie_good_grade = _pick_grade(grades, GRADE_GOOD_FALLBACK)
        est.swappie_fair, est.swappie_fair_grade = _pick_grade(grades, GRADE_FAIR_FALLBACK)

    # --- [FONEDAY] repair cost ---
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

    # --- profit ---
    if est.swappie_fair is not None and est.repair_cost is not None:
        est.break_even = round(est.swappie_fair - est.repair_cost, 2)
        if est.asking_price is not None:
            est.profit_fair = round(
                est.swappie_fair - est.asking_price - est.repair_cost, 2)
            if est.swappie_good is not None:
                est.profit_good = round(
                    est.swappie_good - est.asking_price - est.repair_cost, 2)

    return est
