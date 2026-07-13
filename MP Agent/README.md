# Marktplaats iPhone Monitor

Scans Marktplaats for iPhone 14–17 listings with cheap-to-fix damage
(screen, back cover, charging port, battery, camera lens glass), filters
out noise (business sellers, "wanted to buy" ads, iCloud-locked phones,
water damage, etc.), and pings your phone via Telegram with the ones worth
looking at — including driving distance/time from Veenendaal and a
`[MARKT]` price-context line once enough market data has accumulated.

## How it runs in production

GitHub Actions (`.github/workflows/scan.yml`), triggered by an external
cron-job.org dispatcher (GitHub's own scheduler proved unreliable). State
(the SQLite seen/market database) lives on the repo's `data` branch — the
workflow restores it before each scan and force-pushes the updated
snapshot after. Nothing needs to run on a local machine except the
optional `vault_sync.py` (writes market prices into the Obsidian vault).

## How listings are fetched

`scraper.py` uses Marktplaats' internal LRP search API
(`/lrp/api/search`) as the primary strategy — it's the only path that
honors true date-desc sorting, so every query returns the genuinely
newest 30 listings. HTML parsing (`__NEXT_DATA__` blob, then CSS
selectors) exists strictly as a fallback. Listing detail pages are
fetched only for matches/AI-review cases (`fetch_listing_details`:
description, true posting date, reserved flag) and for market bid/closure
polling (`fetch_listing_status`).

Note (2026-07-13): Marktplaats truncates listing descriptions server-side
at ~230 chars for anonymous requests — the full text is not available
without a login. Filters and the AI classifier are tuned around that.

## Decision pipeline (filters.py + ai_classifier.py)

1. Target model (14–17) named in the title? No → reject.
2. Business/shop signals, buyer ads, paid placements → reject.
3. Hard excludes (iCloud lock, water damage, motherboard...) → reject.
4. Primary damage keyword → accept (unless the title says "lees
   beschrijving" → AI confirms first).
5. Ambiguous damage term surviving negation-phrase stripping → Haiku
   judges it (model: see `ai_model` in config.yaml).
6. No keyword at all but broad damage words survive negation stripping →
   Haiku judges it too.

Every decision (accept/reject + reason) is logged at INFO and stored in
the `reason` column of `seen_listings` — diagnosing "why didn't I get an
alert for X" is one SQL query against the data branch snapshot:

```bash
git fetch origin data
git show "origin/data:MP Agent/seen_listings.db" > /tmp/seen.db
sqlite3 /tmp/seen.db "SELECT matched, reason FROM seen_listings WHERE listing_id='m...'"
```

## Setup (local run)

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill in `ANTHROPIC_API_KEY`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (BotFather → new bot; chat id
   via `https://api.telegram.org/bot<TOKEN>/getUpdates`). Distance uses
   free Nominatim + OSRM — no Maps API key needed.
3. `python main.py` for a manual scan; `python scraper.py` to sanity-check
   extraction alone.

## Tests

```bash
python -m pytest tests/ -q
```

Unit tests over the filter/parsing logic (no network) — run automatically
on push by `.github/workflows/test.yml`. Every historical missed-listing
bug gets a regression test named after the listing that exposed it.

## Tuning the filters

All keyword lists live in `config.yaml` — no code changes needed. Two
things to remember when adding queries: Marktplaats search has **no
stemming** (a "beschadigd" query never matches "beschadigt"), and each
query only returns the newest 30, so high-volume damage words need their
own per-model variants.

## Market-price tracking (market.py + pricebench.py)

All scanned listings plus 4 broad resale-sweep queries feed
`market_listings`/`price_obs` in the same database: asking prices, bids
(polled from listing pages), and a sold-price proxy (listing gone within
7 days with at least one bid). `pricebench.py --summary` reports it;
a weekly workflow posts it to Telegram (Sat 16:00 NL) and the weekly
review updates the vault note.

## What's NOT built yet (by design)

- **Profit/repair-cost estimates**: removed. Swappie payout line went
  2026-07-10, Foneday repair costs went 2026-07-12 (replaced supplier,
  not yet wired in). Both live in git history.
- **Phase 2 profitability filter**: waiting on real flip data + the new
  parts supplier.
- **Playwright/browser automation**: only if plain HTTP stops working.

## Project structure

```
MP Agent/
├── config.yaml           # all tunable settings (queries, keywords, thresholds)
├── .env                  # secrets (never committed)
├── main.py               # orchestration - one scan cycle, run on a schedule
├── scraper.py            # LRP API + listing-page fetching/parsing
├── filters.py            # keyword/exclusion/business/buyer-ad decision logic
├── ai_classifier.py      # Haiku classification for ambiguous cases
├── damage_detect.py      # broad damage detector (recall probe, not a gate)
├── distance.py           # Nominatim + OSRM driving distance (free, no key)
├── telegram_notifier.py  # alert formatting + sending
├── storage.py            # SQLite: seen-dedup, geocode cache, market tables
├── market.py             # market-price tracking (bids, closures, benchmarks)
├── pricebench.py         # CLI price reports (+ Telegram / Obsidian output)
├── models.py             # iPhone model-name parsing (shared)
├── vault_sync.py         # local-only: data branch -> Obsidian vault note
└── tests/                # pytest unit tests (filters, parsing, formatting)
```
