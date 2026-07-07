#!/usr/bin/env python
"""Refresh Foneday parts-price data used by the Phase 2 profit filter.

Logs into foneday.shop with the credentials in .env, enumerates every
repair-relevant part for iPhone 14-17 via the site's Meilisearch index,
pulls your logged-in wholesale price for each SKU, and rewrites:
    data/parts_prices.yaml       - condensed repair-cost table (per model/tier)
    data/parts_prices_compact.json - per model/category: cheapest in-stock
                                     option per quality tier (max 4), sku+price

Run:  python refresh_prices.py     (from the "MP Agent" directory)
Needs in .env:  FONEDAY_EMAIL, FONEDAY_PASSWORD

The Meilisearch host/key below are the site's public browser-search key
(shipped in its front-end JS), not a secret. Only the login is private.
"""
import os, re, json, sys, time, datetime
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.environ.get("FONEDAY_EMAIL")
PASSWORD = os.environ.get("FONEDAY_PASSWORD")
if not (EMAIL and PASSWORD):
    sys.exit("Set FONEDAY_EMAIL and FONEDAY_PASSWORD in .env first.")

BASE = "https://foneday.shop"
MS_HOST = "https://ms-67af32da8d0e-898.fra.meilisearch.io"
MS_KEY = "348c4b30a962c744aa9e4185c05fd4ab059f618d8fca1a373204ee898b83a06d"
MS_INDEX = "articles"
PRICE_URL = f"{BASE}/webshop/quick-search/fetch-article-price-info"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MODELS = [
    "iPhone 14", "iPhone 14 Plus", "iPhone 14 Pro", "iPhone 14 Pro Max",
    "iPhone 15", "iPhone 15 Plus", "iPhone 15 Pro", "iPhone 15 Pro Max",
    "iPhone 16", "iPhone 16 Plus", "iPhone 16 Pro", "iPhone 16 Pro Max",
    "iPhone 17", "iPhone 17 Pro", "iPhone 17 Pro Max",
]
CLASSES = ["Display", "Back Cover", "Charging Connector",
           "Camera Lens", "Camera Glass", "Battery"]
# Foneday classification -> our damage category
CAT = {"Display": "screen", "Back Cover": "back_cover",
       "Charging Connector": "charging_port", "Camera Lens": "camera_lens",
       "Camera Glass": "camera_lens", "Battery": "battery"}
ATTRS = ["id", "sku", "title", "classification", "subCategory", "qualityTitle",
         "qualitySlug", "qualityBadgeAdd", "suitableModels", "colorTitle",
         "onStock", "onSale", "status"]

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def csrf_meta(html: str):
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


def login(client: httpx.Client):
    r = client.get(f"{BASE}/nl/login")
    m = re.search(r'name="_token" value="([^"]+)"', r.text)
    token = m.group(1)
    r = client.post(f"{BASE}/nl/login", data={
        "_token": token, "email": EMAIL, "password": PASSWORD, "rememberme": "1",
    })
    home = client.get(f"{BASE}/nl/home")
    if "Uitloggen" not in home.text and "logout" not in home.text.lower():
        sys.exit("Login failed - check FONEDAY_EMAIL / FONEDAY_PASSWORD.")
    # a catalog page carries a fresh csrf-token meta for the price endpoint
    cat = client.get(f"{BASE}/nl/catalog?category=parts/For-Apple/iPhone/iPhone-14")
    return csrf_meta(cat.text)


def enumerate_parts(client: httpx.Client):
    parts = {}
    for m in MODELS:
        body = {
            "q": "", "limit": 1000,
            "filter": f'suitableModels = "{m}" AND classification IN {json.dumps(CLASSES)}',
            "attributesToRetrieve": ATTRS,
        }
        r = client.post(f"{MS_HOST}/indexes/{MS_INDEX}/search",
                        headers={"Authorization": f"Bearer {MS_KEY}"}, json=body)
        hits = r.json().get("hits", [])
        for h in hits:
            sku = h.get("sku")
            if not sku:
                continue
            if sku not in parts:
                h["_models"] = []
                parts[sku] = h
            if m not in parts[sku]["_models"]:
                parts[sku]["_models"].append(m)
        print(f"  {m:<22} {len(hits):4d} parts", file=sys.stderr)
    return parts


def fetch_prices(client: httpx.Client, csrf, skus):
    prices = {}
    for i in range(0, len(skus), 50):
        batch = skus[i:i + 50]
        r = client.post(PRICE_URL, json={"skus": batch}, headers={
            "X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"})
        prices.update(r.json().get("articlePriceInfos", {}))
        time.sleep(0.3)
    return prices


def pf(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build_outputs(parts):
    for sku, p in parts.items():
        pi = p.pop("_price", None)
    # bucket per model+category
    buckets = {m: {} for m in MODELS}
    for p in parts.values():
        cat = CAT.get(p.get("classification"))
        if not cat:
            continue
        price = pf(p.get("discountprice") or p.get("price"))
        if price is None:
            continue
        rec = {"price": price, "instock": bool(p.get("onStock")),
               "title": p.get("title") or ""}
        for m in p.get("_models", []):
            if m in buckets:
                buckets[m].setdefault(cat, []).append(rec)

    def cheapest(recs, pred=lambda r: True):
        pool = [r for r in recs if pred(r)]
        instock = [r for r in pool if r["instock"]]
        pool = instock or pool
        return min(pool, key=lambda r: r["price"])["price"] if pool else None

    def oled(r): return "oled" in r["title"].lower()
    def incell(r): return "in-cell" in r["title"].lower() or "incell" in r["title"].lower()

    today = datetime.date.today().isoformat()
    L = [
        "# Foneday parts prices - repair-cost inputs for Phase 2 profit filter",
        f"# Generated {today} from foneday.shop customer pricing.",
        "# Prices EUR, wholesale (logged-in price), cheapest IN-STOCK option per tier.",
        "# Regenerate with: python refresh_prices.py  (needs FONEDAY_* in .env)",
        "#",
        "# screen_incell = cheap aftermarket LCD/in-cell (no true-tone; budget flips)",
        "# screen_oled   = OLED display (looks like original; safer resale)",
        "# back_cover    = cheapest back-cover/housing (Pro = full housing, pricey)",
        "# camera_lens   = camera glass/lens only (cheap glue-on fix)",
        "",
        "meta:",
        f"  generated: {today}",
        "  source: foneday.shop",
        "  currency: EUR",
        f"  total_parts_indexed: {len(parts)}",
        "",
        "repair_costs:",
    ]

    def v(x): return f"{x:.2f}" if x is not None else "null"
    for m in MODELS:
        b = buckets[m]
        scr = b.get("screen", [])
        L.append(f'  "{m.lower()}":')
        L.append(f'    screen_incell: {v(cheapest(scr, incell))}')
        L.append(f'    screen_oled: {v(cheapest(scr, oled))}')
        L.append(f'    back_cover: {v(cheapest(b.get("back_cover", [])))}')
        L.append(f'    charging_port: {v(cheapest(b.get("charging_port", [])))}')
        L.append(f'    camera_lens: {v(cheapest(b.get("camera_lens", [])))}')
        L.append(f'    battery: {v(cheapest(b.get("battery", [])))}')

    (DATA_DIR / "parts_prices.yaml").write_text("\n".join(L) + "\n", encoding="utf-8")
    (DATA_DIR / "parts_prices_compact.json").write_text(
        json.dumps(build_compact(parts.values()), ensure_ascii=False, indent=1),
        encoding="utf-8")


def build_compact(parts):
    """Per model x category: cheapest in-stock option per quality tier, max 4.

    Keeps just enough to order the part (sku, quality, price) and to see
    tier alternatives when the cheapest is a quality risk.
    """
    models = {m.lower(): {} for m in MODELS}
    for p in parts:
        cat = CAT.get(p.get("classification"))
        price = pf(p.get("discountprice") or p.get("price"))
        if not cat or price is None or not p.get("onStock"):
            continue
        rec = {"sku": p.get("sku"), "quality": p.get("qualityTitle") or "standard",
               "price": price}
        for m in p.get("_models", []):
            cats = models.get(m.lower())
            if cats is not None:
                cats.setdefault(cat, []).append(rec)

    for cats in models.values():
        for cat, recs in cats.items():
            best = {}
            for r in sorted(recs, key=lambda r: r["price"]):
                best.setdefault(r["quality"], r)
            cats[cat] = sorted(best.values(), key=lambda r: r["price"])[:4]

    return {"meta": {"generated": datetime.date.today().isoformat(),
                     "source": "foneday.shop", "currency": "EUR",
                     "note": "cheapest in-stock per quality tier, max 4 per category"},
            "models": models}


def main():
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=30) as client:
        print("Logging in...", file=sys.stderr)
        csrf = login(client)
        print("Enumerating parts...", file=sys.stderr)
        parts = enumerate_parts(client)
        skus = list(parts.keys())
        print(f"Pricing {len(skus)} SKUs...", file=sys.stderr)
        prices = fetch_prices(client, csrf, skus)
        priced = 0
        for sku, p in parts.items():
            pi = prices.get(sku)
            if pi:
                p["price"] = pi.get("price")
                p["discountprice"] = pi.get("discountprice")
                p["currency"] = pi.get("currency")
                p["bulkprices"] = pi.get("bulkprices")
                priced += 1
            else:
                p["price"] = None
        print(f"Priced {priced}/{len(skus)}", file=sys.stderr)
        build_outputs(parts)
        print("Wrote data/parts_prices.yaml and data/parts_prices_compact.json", file=sys.stderr)


if __name__ == "__main__":
    main()
