# Marktplaats iPhone Monitor

Scans Marktplaats for iPhone 14/15/16 listings with cheap-to-fix damage
(screen, back cover, charging port), filters out noise (business sellers,
"wanted to buy" ads, iCloud-locked phones, water damage, etc.), and pings
your phone via Telegram with the ones worth looking at - including driving
distance/time from Veenendaal.

## ⚠️ Before you run this - read this part

I built the scraper (`scraper.py`) defensively, but **I could not test it
against the live Marktplaats site from my sandboxed environment**
(marktplaats.nl isn't reachable from where this was built). It uses two
extraction strategies:

1. Looks for an embedded JSON data blob in the page (reliable, if present)
2. Falls back to CSS-selector scraping (the selectors are my best guess
   and may need adjusting)

**Your first step should be:**
```bash
python scraper.py
```
This runs a test fetch and prints what it found. If it prints 0 listings,
open a Marktplaats search URL in Chrome, right-click a listing title →
"Inspect", and update the `SELECTORS` dict at the top of `scraper.py` to
match what you see. This is the one part of this project that depends on
Marktplaats' current page structure, which changes over time - expect to
revisit it occasionally.

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Get your API keys / tokens:**
   - **Anthropic API key**: console.anthropic.com → Settings → API Keys
   - **Telegram bot**: message `@BotFather` on Telegram → `/newbot` → follow
     prompts → copy the token
   - **Telegram chat ID**: send any message to your new bot, then visit
     `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
     find the numeric `"chat":{"id": ...}` value
   - **Google Maps API key**: Google Cloud Console → enable "Distance
     Matrix API" → create an API key

3. **Configure secrets:**
   ```bash
   cp .env.example .env
   # then edit .env and fill in your real values
   ```

4. **Test the scraper** (see warning above):
   ```bash
   python scraper.py
   ```

5. **Do a manual test run:**
   ```bash
   python main.py
   ```
   Check `Settings > Usage` won't apply here since this uses the API, not
   Claude.ai - instead watch the Anthropic Console's usage page for actual
   spend, which should be a few cents at most for a single run.

6. **Schedule it** (Linux/Mac cron example, every 3 hours):
   ```bash
   crontab -e
   ```
   Add:
   ```
   0 */3 * * * cd /full/path/to/marktplaats_monitor && /usr/bin/python3 main.py >> run.log 2>&1
   ```
   On Windows, use Task Scheduler instead, pointing at `main.py` with the
   same interval.

## Tuning the filters

All the keyword lists live in `config.yaml` - no code changes needed to
adjust what counts as a match, an exclusion, or noise. Edit and re-run.

## What's NOT built yet (by design - see project plan)

- **Interactive "tap for OV route" button**: requires the bot to run as a
  persistent process (webhook/long-polling), not a cron script that exits.
  `distance.get_transit_time()` exists and works, it's just not wired into
  the automated flow yet.
- **Negotiation/reply drafting**: planned for later phases once this core
  monitoring loop is proven out.
- **Profit-based filtering**: alerts now *show* an estimated flip profit
  (see below) but nothing is filtered on it yet - that needs real flip
  data first to calibrate against.
- **Playwright/browser automation**: only add this if the plain HTTP
  fetch approach in `scraper.py` stops working reliably (e.g. Marktplaats
  adds bot detection) - it's a heavier, slower fallback, not the default.

## Phase 2: profit estimate in alerts

Each alert now includes a best-effort flip calculation:

```
💶 Swappie resale [SWAPPIE]: €435 (Heel goed) / €425 (Redelijk) · 128GB
🔩 Repair est. [FONEDAY]: €29.95 (screen)
📈 Profit after repair: €155 (Good) / €145 (Fair)
```

**Profit = [SWAPPIE] resale − asking price − [FONEDAY] repair cost.**

- **[SWAPPIE] resale** (`data/swappie_prices.json`): Swappie's public
  catalog price for a refurbished phone of that model/storage, grades
  C "Heel goed" (Good) and D "Redelijk" (Fair). That's the *retail
  ceiling* - price a repaired flip slightly under it to sell fast.
  Refresh with `python refresh_swappie_prices.py` (no login needed) or
  via the weekly `refresh-swappie.yml` workflow.
- **[FONEDAY] repair** (`data/parts_prices.yaml`): wholesale part cost
  for the damage type detected in the listing text. Screen repairs
  assume the OLED tier by default (`screen_repair_tier` in config.yaml,
  switch to `screen_incell` for budget flips).
- Listings with no asking price ("Bieden") get a **break-even max bid**
  instead: bid above that and the flip loses money even at Fair resale.
- Anything unparseable (unknown model, 16e/Air, missing data) just drops
  that line from the alert - profit info never blocks a notification.

## Cost expectations

- **Anthropic API**: only ambiguous ("mankement") listings get sent to
  Claude Haiku for classification - most listings resolve via free keyword
  matching. Expect roughly €3-5/month at a scan-every-2-3-hours cadence.
- **Google Maps Distance Matrix**: free tier covers this volume easily at
  a few scans/day.
- **Telegram**: free.

## Project structure

```
marktplaats_monitor/
├── config.yaml           # all tunable settings (keywords, models, etc)
├── .env                  # secrets (you create this, never commit it)
├── main.py               # orchestration - run this on a schedule
├── scraper.py            # fetches + parses Marktplaats search pages
├── filters.py            # keyword/exclusion/business/buyer-ad logic
├── ai_classifier.py      # Haiku-based classification for ambiguous cases
├── distance.py           # Google Maps driving/transit distance
├── telegram_notifier.py  # sends the actual phone notifications
├── storage.py            # SQLite dedup tracking
├── profit.py             # Phase 2: [SWAPPIE] resale - asking - repair
├── refresh_swappie_prices.py  # refreshes data/swappie_prices.json
├── refresh_prices.py     # refreshes Foneday repair-part prices
├── data/                 # committed price data (Swappie + Foneday)
└── seen_listings.db      # created automatically on first run
```
