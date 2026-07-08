#!/usr/bin/env python
"""Refresh [SWAPPIE] resale-price data used by the Phase 2 profit estimate.

Swappie (swappie.com) is the biggest refurbished-iPhone shop in the EU;
what they charge for a refurbished phone in a given condition is the
realistic ceiling of what a repaired flip resells for, and their grade D
("Redelijk"/Fair) price is the conservative benchmark for a quick private
sale on Marktplaats. This pulls their public per-model catalog API:

    GET https://swappie.com/api/model/nl/{Model Name}

No login or API key needed - it's the exact JSON their own product page
preloads (found as <link href="/api/model/nl/iPhone 15" as="fetch"> on
https://swappie.com/nl/model/iphone-15/). It returns every in-stock
variant (grade x storage x color) with its current price in cents.

We keep the CHEAPEST in-stock price per grade+storage (min across colors,
buyers don't pay a premium for color on a flip) and write:
    data/swappie_prices.json

Swappie NL condition grades:
    A = Premium   (als nieuw / like new)
    B = Uitstekend (excellent)
    C = Heel goed  (good)
    D = Redelijk   (fair)

NOTE: these are Swappie's RETAIL prices (what a buyer pays Swappie,
incl. their 12-month warranty). What Swappie would PAY for a phone via
their trade-in flow sits behind a session-based questionnaire with no
public endpoint - and it's much lower anyway. For flip decisions the
retail price is the right benchmark: price your repaired phone slightly
under Swappie's grade C/D and it's competitive.

Run:  python refresh_swappie_prices.py     (from the "MP Agent" directory)
"""
import datetime
import json
import sys
from pathlib import Path

import httpx

API = "https://swappie.com/api/model/nl/{model}"
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


def main() -> None:
    out = {}
    with httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"},
                      timeout=30) as client:
        for model in MODELS:
            try:
                by_storage = fetch_model(client, model)
            except httpx.HTTPError as exc:
                print(f"  {model:<22} FAILED: {exc}", file=sys.stderr)
                continue
            n = sum(len(g) for g in by_storage.values())
            print(f"  {model:<22} {len(by_storage)} storages, "
                  f"{n} grade prices", file=sys.stderr)
            if by_storage:
                out[model.lower()] = by_storage

    payload = {
        "meta": {
            "generated": datetime.date.today().isoformat(),
            "source": "swappie.com/api/model/nl (public catalog API) [SWAPPIE]",
            "currency": "EUR",
            "price_kind": "Swappie RETAIL price (refurbished, 12mo warranty), "
                          "cheapest in-stock variant per grade+storage",
            "grades": GRADE_LABELS,
        },
        "models": out,
    }
    path = DATA_DIR / "swappie_prices.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"Wrote {path} ({len(out)}/{len(MODELS)} models)", file=sys.stderr)


if __name__ == "__main__":
    main()
