#!/usr/bin/env python
"""Refresh [SWAPPIE] price data used by the Phase 2 profit estimate.

Two datasets, both public (no login or API key), both written to
data/swappie_prices.json:

1. SELL / trade-in payout ("verkoop" menu) - what Swappie PAYS you for a
   phone. This is what the profit estimate uses: it's the guaranteed
   exit for a repaired flip and matches the prices you see on
   https://swappie.com/nl/verkoop-iphone/.

       GET https://swappie.com/api/sell/api/v3/prices/
           ?model_name=iPhone 14&country=NL
           &storages=["64GB","128GB","256GB","512GB","1TB"]

   (Found in Swappie's sell-flow JS bundle; it's the call behind the
   questionnaire.) Returns a price per storage x visual condition x set
   of functional defects. We keep only the FULLY WORKING rows
   (functional_condition == []) because a flip is repaired before
   selling, per visual condition:

       LIKE_NEW   = Als nieuw     ALMOST_NEW = Bijna nieuw
       GOOD       = Goed          MODERATE   = Matig

   SEALED_BOX is skipped - a flip is never sealed.

2. RETAIL catalog price - what a buyer pays Swappie for a refurbished
   phone (incl. 12-month warranty). Kept as a reference ceiling for
   pricing a flip on Marktplaats, NOT used in the profit math.

       GET https://swappie.com/api/model/nl/{Model Name}

   Cheapest in-stock variant per grade+storage (min across colors).
   Grades: A = Premium, B = Uitstekend, C = Heel goed, D = Redelijk.

Run:  python refresh_swappie_prices.py     (from the "MP Agent" directory)
"""
import datetime
import json
import re
import sys
from pathlib import Path

import httpx

API = "https://swappie.com/api/model/nl/{model}"
SELL_API = "https://swappie.com/api/sell/api/v3/prices/"
# Every storage Swappie has ever sold for these generations; the API
# silently ignores sizes a model doesn't come in.
SELL_STORAGES = ["64GB", "128GB", "256GB", "512GB", "1TB"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Same model set as refresh_prices.py (Foneday) so profit.py can join the
# two datasets on the lowercase model name. No iPhone Air / 16e / 17e -
# those are excluded by the scraper's filters anyway.
MODELS = [
    "iPhone 14", "iPhone 14 Plus", "iPhone 14 Pro", "iPhone 14 Pro Max",
    "iPhone 15", "iPhone 15 Plus", "iPhone 15 Pro", "iPhone 15 Pro Max",
    "iPhone 16", "iPhone 16 Plus", "iPhone 16 Pro", "iPhone 16 Pro Max",
    "iPhone 17", "iPhone 17 Pro", "iPhone 17 Pro Max",
]

GRADE_LABELS = {"A": "Premium (als nieuw)", "B": "Uitstekend",
                "C": "Heel goed", "D": "Redelijk"}

SELL_CONDITION_LABELS = {"LIKE_NEW": "Als nieuw", "ALMOST_NEW": "Bijna nieuw",
                         "GOOD": "Goed", "MODERATE": "Matig"}

_SELL_STORAGE_RE = re.compile(r"(\d+)\s?(GB|TB)\s*$", re.IGNORECASE)

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def fetch_model(client: httpx.Client, model: str) -> dict:
    """Return {storage(str): {grade: cheapest_price_eur}} for one model."""
    r = client.get(API.format(model=model))
    r.raise_for_status()
    phones = r.json().get("availablePhones", [])

    by_storage: dict = {}
    for p in phones:
        grade = p.get("grade")
        storage = p.get("storage")
        price = (p.get("normalPrice") or {}).get("amount")
        stock = p.get("stock", 0)
        if not (grade and storage and price) or stock <= 0:
            continue
        eur = price / 100  # API amounts are cents (precision 2)
        slot = by_storage.setdefault(str(storage), {})
        if grade not in slot or eur < slot[grade]:
            slot[grade] = eur
    return by_storage


def fetch_sell_prices(client: httpx.Client, model: str) -> dict:
    """Return {storage(str): {visual_condition: payout_eur}} for one model.

    Only fully working rows (no functional defects) - the flip is repaired
    before it's sold to Swappie, so that's the payout that applies.
    """
    r = client.get(SELL_API, params={
        "model_name": model,
        "country": "NL",
        "storages": json.dumps(SELL_STORAGES),
    })
    r.raise_for_status()
    rows = r.json().get("results", [])

    by_storage: dict = {}
    for row in rows:
        if row.get("functional_condition"):     # has defects - not our exit
            continue
        cond = row.get("visual_condition")
        if cond not in SELL_CONDITION_LABELS:   # skips SEALED_BOX etc.
            continue
        m = _SELL_STORAGE_RE.search(row.get("model_name", ""))
        price = (row.get("price") or {}).get("price")
        if not (m and price):
            continue
        gb = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
        by_storage.setdefault(str(gb), {})[cond] = float(price)
    return by_storage


def main() -> None:
    retail, sell = {}, {}
    with httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"},
                      timeout=30) as client:
        for model in MODELS:
            try:
                by_storage = fetch_model(client, model)
            except httpx.HTTPError as exc:
                print(f"  {model:<22} retail FAILED: {exc}", file=sys.stderr)
                by_storage = {}
            try:
                sell_by_storage = fetch_sell_prices(client, model)
            except httpx.HTTPError as exc:
                print(f"  {model:<22} sell FAILED: {exc}", file=sys.stderr)
                sell_by_storage = {}
            print(f"  {model:<22} retail: {len(by_storage)} storages, "
                  f"sell: {len(sell_by_storage)} storages", file=sys.stderr)
            if by_storage:
                retail[model.lower()] = by_storage
            if sell_by_storage:
                sell[model.lower()] = sell_by_storage

    payload = {
        "meta": {
            "generated": datetime.date.today().isoformat(),
            "source": "swappie.com public APIs [SWAPPIE]: "
                      "/api/sell/api/v3/prices/ (trade-in payout) + "
                      "/api/model/nl (retail catalog)",
            "currency": "EUR",
            "sell_price_kind": "what Swappie PAYS for a fully working phone "
                               "(verkoop/trade-in flow), per visual condition",
            "sell_conditions": SELL_CONDITION_LABELS,
            "retail_price_kind": "Swappie RETAIL price (refurbished, 12mo "
                                 "warranty), cheapest in-stock variant per "
                                 "grade+storage - reference ceiling only",
            "retail_grades": GRADE_LABELS,
        },
        "sell_models": sell,
        "models": retail,
    }
    path = DATA_DIR / "swappie_prices.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"Wrote {path} (sell: {len(sell)}/{len(MODELS)}, "
          f"retail: {len(retail)}/{len(MODELS)} models)", file=sys.stderr)


if __name__ == "__main__":
    main()
