"""
market.py

Realistic market-price tracking for iPhones on Marktplaats - based on what
listings ACTUALLY go for (bids + listings that disappear = sale proxy),
not the asking prices of stale listings nobody buys.

Data model (tables created in storage.init_db, same seen_listings.db file
so the data-branch snapshot carries everything):

  market_listings - one row per listing with a parseable iPhone model,
                    both damaged (buy side) and working (resale side).
  price_obs       - append-only price/bid observations over time.

Three signals, in increasing order of realism:
  1. Asking prices  - available immediately, but optimistic.
  2. Bids           - real money offered; scraped from the listing page's
                      __NEXT_DATA__ "bids" array (scraper.fetch_listing_status).
  3. Closed prices  - a listing that disappears within days WITH bids on it
                      very likely sold; its last ask / highest bid is the
                      best sale-price proxy Marktplaats offers (there is no
                      public sold-price data). Listings that sat for weeks
                      and vanished probably just expired - reported
                      separately, never mixed into "sold" stats.

Everything here is best-effort and must never kill a scan: all entry
points called from main.py catch and log their own exceptions.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median, quantiles
from typing import Optional

import filters
import models
import scraper
import storage

logger = logging.getLogger(__name__)

# A closed listing counts as a likely SALE (not expiry) when it had at
# least one bid and was gone within this many days of first sighting.
SALE_MAX_DAYS = 7

_STORAGE_RE = re.compile(r"(\d+)\s*(GB|TB)", re.IGNORECASE)

# Damage vocabulary for the is_damaged split. The config primary_keywords
# are checked too (ingest gets config), but these broad stems catch damaged
# listings from the resale-side queries whose phrasing the precise keyword
# list misses - better to wrongly call a working phone "damaged" than to
# pollute the resale benchmark with broken ones.
_DAMAGE_STEMS = (
    "kapot", "defect", "schade", "beschadig", "gebarsten", "barst",
    "gebroken", "stuk", "voor onderdelen", "voor reparatie", "krasje",
    "krassen", "gebutst", "deuk", "werkt niet", "doet het niet",
    "laadt niet", "mankement", "gebrek",
)


# Negation prefixes that turn a damage stem into a SELLING POINT.
# Generated combinatorially against _DAMAGE_STEMS rather than hand-listed,
# so adding a stem above automatically gets its negated forms covered -
# hand-maintained negation lists are exactly how the original bug survived.
_NEGATION_PREFIXES = ("geen", "zonder", "nooit", "niet", "vrij van")

# Negations that aren't "<prefix> <stem>" shaped and so can't be generated.
_EXTRA_NEGATIONS = (
    "krasvrij", "krasjevrij", "schadevrij", "barstvrij",
    "geen enkele kras", "geen enkele schade", "geen enkel gebrek",
    "geen gebruikerssporen", "geen gebruiks sporen", "amper krassen",
    "nauwelijks krassen", "geen zichtbare schade", "geen enkele deuk",
)


def _negation_phrases(config: dict) -> list[str]:
    """Config negations + every "<prefix> <damage stem>" combination."""
    phrases = list(config.get("negation_phrases", []))
    phrases.extend(_EXTRA_NEGATIONS)
    for stem in _DAMAGE_STEMS:
        for prefix in _NEGATION_PREFIXES:
            phrases.append(f"{prefix} {stem}")
    # Longest first: "geen schade aan scherm" must be consumed before the
    # shorter "geen schade" eats only part of it and leaves debris behind.
    return sorted(set(phrases), key=len, reverse=True)


def strip_negations(text: str, config: dict) -> str:
    """Remove negated-damage phrasing so only real damage claims remain."""
    return filters.strip_negation_phrases(text.lower(), _negation_phrases(config))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_storage_gb(storage_text: str, title: str = "") -> Optional[int]:
    """'128 GB' -> 128; '1 TB' -> 1024; falls back to the title text."""
    for text in (storage_text, title):
        m = _STORAGE_RE.search(text or "")
        if m:
            value = int(m.group(1))
            return value * 1024 if m.group(2).upper() == "TB" else value
    return None


def _is_damaged(listing, config: dict) -> bool:
    """Damaged (buy side) vs working (resale side), for the price benchmark.

    NEGATION-AWARE (fixed 2026-07-23). This was the last negation-blind
    substring check in the codebase - the same bug class already fixed
    three times in filters.py, hiding here where it silently corrupted
    data instead of dropping alerts.

    Sellers of clean working phones advertise the ABSENCE of damage
    constantly, so "geen schade" matched the 'schade' stem, "zonder
    krassen" matched 'krassen', "geen gebreken" matched 'gebrek'. Real
    production rows misfiled as damaged: "iPhone 15 Pro Max 512GB | 100%
    Batterij geen schade + doos" (EUR 850), "Iphone 15 Pro 256GB | Natural
    Titanium | Zonder krassen" (EUR 549), "iPhone 17 Pro max (zonder
    schade) %100 batterij" (EUR 1200).

    The bias ran one way and it mattered: the phones being pulled out of
    the werkend pool were the cleanest, best-described, HIGHEST-priced
    ones, so the resale median every buy decision is measured against
    came out too low - making every deal look worse than it really is.
    Stripping negations first is exactly what filters.py already does.
    """
    text = strip_negations(listing.combined_text, config)
    if any(stem in text for stem in _DAMAGE_STEMS):
        return True
    return any(kw in text for kw in config.get("primary_keywords", []))


# --- Ingest -----------------------------------------------------------------

def ingest_listings(listings, config: dict, db_path: Path = storage.DB_PATH) -> None:
    """Upsert every listing with a parseable model; record a price
    observation when the ask price changed. Called for every scanned query
    (damage queries AND the market_queries resale sweep). Never raises."""
    try:
        _ingest(listings, config, db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Market ingest failed: %s", exc)


def _ingest(listings, config: dict, db_path: Path) -> None:
    now = _now()
    with sqlite3.connect(db_path) as conn:
        for listing in listings:
            model = models.parse_model(listing.title)
            if not model or not listing.listing_id:
                continue
            row = conn.execute(
                "SELECT final_ask_cents, status FROM market_listings WHERE listing_id = ?",
                (listing.listing_id,),
            ).fetchone()
            ask = listing.price_cents or None
            if row is None:
                conn.execute(
                    """
                    INSERT INTO market_listings
                        (listing_id, model, storage_gb, condition, is_damaged,
                         price_type, title, url, first_seen_utc, last_seen_utc,
                         status, final_ask_cents)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        listing.listing_id, model,
                        parse_storage_gb(listing.storage_text, listing.title),
                        listing.condition, int(_is_damaged(listing, config)),
                        listing.price_type, listing.title, listing.url,
                        now, now, ask,
                    ),
                )
                if ask:
                    conn.execute(
                        "INSERT INTO price_obs (listing_id, ts_utc, ask_cents) VALUES (?, ?, ?)",
                        (listing.listing_id, now, ask),
                    )
            else:
                prev_ask, status = row
                # A listing we thought was gone showing up again = relist/bump.
                conn.execute(
                    """
                    UPDATE market_listings
                    SET last_seen_utc = ?, status = 'open', closed_utc = NULL,
                        final_ask_cents = COALESCE(?, final_ask_cents)
                    WHERE listing_id = ?
                    """,
                    (now, ask, listing.listing_id),
                )
                if ask and ask != prev_ask:
                    conn.execute(
                        "INSERT INTO price_obs (listing_id, ts_utc, ask_cents) VALUES (?, ?, ?)",
                        (listing.listing_id, now, ask),
                    )
        conn.commit()


# --- Bid polling ------------------------------------------------------------

def poll_bids(config: dict, db_path: Path = storage.DB_PATH) -> None:
    """Fetch detail pages for open bidding-type listings (oldest-checked
    first, capped per run) and record their current bids. Never raises."""
    try:
        _poll_bids(config, db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Market bid polling failed: %s", exc)


def _poll_bids(config: dict, db_path: Path) -> None:
    cap = config.get("market_bid_poll_per_run", 20)
    user_agent = config["user_agent"]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT listing_id, url, final_bid_cents, bid_count FROM market_listings
            WHERE status = 'open' AND price_type IN ('MIN_BID', 'FAST_BID')
            ORDER BY last_bid_check_utc IS NOT NULL, last_bid_check_utc ASC
            LIMIT ?
            """,
            (cap,),
        ).fetchall()

    checked = 0
    for listing_id, url, prev_bid, prev_count in rows:
        try:
            status = scraper.fetch_listing_status(url, user_agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bid check failed for %s: %s", listing_id, exc)
            continue
        checked += 1
        now = _now()
        with sqlite3.connect(db_path) as conn:
            if status.gone:
                _close_listing(conn, listing_id, now)
            elif status.bid_cents is not None:
                highest = status.bid_cents[0] if status.bid_cents else None
                conn.execute(
                    """
                    UPDATE market_listings
                    SET last_bid_check_utc = ?,
                        final_bid_cents = NULLIF(MAX(COALESCE(final_bid_cents, 0), COALESCE(?, 0)), 0),
                        bid_count = MAX(bid_count, ?)
                    WHERE listing_id = ?
                    """,
                    (now, highest, len(status.bid_cents), listing_id),
                )
                if highest and (highest != prev_bid or len(status.bid_cents) != prev_count):
                    conn.execute(
                        """
                        INSERT INTO price_obs (listing_id, ts_utc, highest_bid_cents, bid_count)
                        VALUES (?, ?, ?, ?)
                        """,
                        (listing_id, now, highest, len(status.bid_cents)),
                    )
            else:
                # Page live but bids unparseable - just move it to the back
                # of the queue.
                conn.execute(
                    "UPDATE market_listings SET last_bid_check_utc = ? WHERE listing_id = ?",
                    (now, listing_id),
                )
            conn.commit()
        time.sleep(0.5)
    if checked:
        logger.info("Market: bid-checked %d listings", checked)


# --- Closure detection ------------------------------------------------------

def _close_listing(conn: sqlite3.Connection, listing_id: str, now: str) -> None:
    conn.execute(
        """
        UPDATE market_listings
        SET status = 'gone', closed_utc = ?
        WHERE listing_id = ? AND status = 'open'
        """,
        (now, listing_id),
    )


def check_closures(config: dict, db_path: Path = storage.DB_PATH) -> None:
    """Listings unseen for market_stale_hours get one detail-page check:
    gone -> closed (sale proxy), still live -> last_seen bumped so it isn't
    rechecked immediately. Capped per run. Never raises."""
    try:
        _check_closures(config, db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Market closure check failed: %s", exc)


def _check_closures(config: dict, db_path: Path) -> None:
    cap = config.get("market_closure_checks_per_run", 10)
    stale_hours = config.get("market_stale_hours", 48)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()
    user_agent = config["user_agent"]

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT listing_id, url FROM market_listings
            WHERE status = 'open' AND last_seen_utc < ?
            ORDER BY last_seen_utc ASC
            LIMIT ?
            """,
            (cutoff, cap),
        ).fetchall()

    closed = 0
    for listing_id, url in rows:
        try:
            status = scraper.fetch_listing_status(url, user_agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Closure check failed for %s: %s", listing_id, exc)
            continue
        now = _now()
        with sqlite3.connect(db_path) as conn:
            if status.gone:
                _close_listing(conn, listing_id, now)
                closed += 1
            else:
                # Still live, just fell out of the newest-30 window. Bump
                # last_seen so it goes to the back of the recheck queue,
                # and take the free bid reading while we're here.
                highest = status.bid_cents[0] if status.bid_cents else None
                conn.execute(
                    """
                    UPDATE market_listings
                    SET last_seen_utc = ?,
                        final_bid_cents = NULLIF(MAX(COALESCE(final_bid_cents, 0), COALESCE(?, 0)), 0),
                        bid_count = MAX(bid_count, COALESCE(?, 0))
                    WHERE listing_id = ?
                    """,
                    (now, highest, len(status.bid_cents or []), listing_id),
                )
            conn.commit()
        time.sleep(0.5)
    if rows:
        logger.info("Market: closure-checked %d listings, %d gone", len(rows), closed)


# --- Benchmarks -------------------------------------------------------------

def benchmark(
    model: str,
    storage_gb: Optional[int] = None,
    damaged: Optional[bool] = None,
    window_days: int = 30,
    db_path: Path = storage.DB_PATH,
) -> dict:
    """Return price stats (euros) for one model segment:
    {
      "n_open", "ask_median", "ask_p25", "ask_p75",
      "n_bids", "bid_median",
      "n_sold", "sold_median",     # gone within SALE_MAX_DAYS with >=1 bid
      "n_expired",                 # gone without bids / after sitting long
    }
    Missing stats are None. `damaged=None` means both sides combined.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    where = "model = ? AND last_seen_utc >= ?"
    params: list = [model, cutoff]
    if storage_gb is not None:
        where += " AND storage_gb = ?"
        params.append(storage_gb)
    if damaged is not None:
        where += " AND is_damaged = ?"
        params.append(int(damaged))

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT status, final_ask_cents, final_bid_cents, bid_count,
                   first_seen_utc, closed_utc
            FROM market_listings WHERE {where}
            """,
            params,
        ).fetchall()

    asks, bids, sold, expired = [], [], [], 0
    for status_, ask, bid, bid_count, first_seen, closed_utc in rows:
        if status_ == "open":
            if ask:
                asks.append(ask / 100)
            if bid:
                bids.append(bid / 100)
        else:
            days_listed = None
            if closed_utc and first_seen:
                days_listed = (
                    datetime.fromisoformat(closed_utc) - datetime.fromisoformat(first_seen)
                ).total_seconds() / 86400
            if bid_count and days_listed is not None and days_listed <= SALE_MAX_DAYS:
                # Likely a real sale: best proxy = highest of last ask / top bid
                sold.append(max(ask or 0, bid or 0) / 100)
            else:
                expired += 1

    def _stats(values):
        if not values:
            return None, None, None
        if len(values) < 4:
            return median(values), None, None
        q = quantiles(values, n=4)
        return median(values), q[0], q[2]

    ask_median, ask_p25, ask_p75 = _stats(asks)
    return {
        "n_open": len(asks),
        "ask_median": ask_median, "ask_p25": ask_p25, "ask_p75": ask_p75,
        "n_bids": len(bids),
        "bid_median": median(bids) if bids else None,
        "n_sold": len(sold),
        "sold_median": median(sold) if sold else None,
        "n_expired": expired,
    }


# --- Deal scoring -----------------------------------------------------------
#
# WHY THIS EXISTS (2026-07-23). The pipeline judged listings purely on
# DAMAGE WORDING and never on price. But "a great find" is defined by price
# - a 15 Pro with a cracked back at EUR 180 and one at EUR 480 produce
# identical-looking alerts, and at ~30 alerts a day the cheap one drowns.
# Milad's "the phones I bought in the beginning were great, I don't get
# those finds anymore" is partly this: the good ones are still coming
# through, they just aren't marked, so they get skimmed past.
#
# We already collect everything needed (werkend asking prices, bids, and
# sale proxies) for the [MARKT] line - this turns that data from passive
# context into an actual verdict, at zero extra requests.

# Headroom = (realistic resale) - (asking price), as a share of resale.
# Gross, BEFORE repair cost - the repair-cost source was removed from the
# repo (Foneday, 07-12), so this deliberately does not pretend to know net
# profit. Thresholds are set so "goed" still clears a typical screen or
# back-glass repair with room left over.
# The last threshold MUST be -inf, not 0.0. A listing priced ABOVE its
# model's resale value has negative headroom, which matched no tier at all
# and made the lookup below raise StopIteration - caught and swallowed as
# "no score", so overpriced listings silently lost their price line
# instead of being labelled "❌ Te duur". Caught in live verification
# 2026-07-23 against real listings; TestDealScore covers it now.
DEAL_TIERS = (
    (0.55, "🔥 TOPDEAL"),
    (0.40, "✅ Goede marge"),
    (0.25, "➖ Krappe marge"),
    (float("-inf"), "❌ Te duur"),
)

# Asking prices are optimistic; a realistic private sale lands under the
# median ask. Only used when we have no sale-proxy data to go on.
ASK_TO_SALE_FACTOR = 0.90


def _resale_ask_pool(model: str, window_days: int, db_path: Path) -> list:
    """Every WERKEND asking price seen for this model inside the window,
    whether or not the listing is still live.

    benchmark()'s ask stats deliberately cover only OPEN listings - that's
    the right question for "what's on the shelf right now". It's the wrong
    question here: a listing that sold last week was still a real market
    asking price, and dropping it left the deal reference resting on a
    handful of rows (live check 2026-07-23: n=6 for the iPhone 15, on
    ~100 werkend rows collected). A reference that thin swings by tens of
    euros as single listings come and go, which would make the deal
    verdict jump around for no real reason.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT final_ask_cents FROM market_listings
            WHERE model = ? AND is_damaged = 0 AND last_seen_utc >= ?
              AND final_ask_cents IS NOT NULL AND final_ask_cents > 0
            """,
            (model, cutoff),
        ).fetchall()
    return [row[0] / 100 for row in rows]


def resale_reference(model: str, config: dict, db_path: Path = storage.DB_PATH):
    """Realistic WERKEND resale price (euros) for a model, or None.

    Prefers the sale proxy (listings that vanished fast with bids on them)
    over asking prices, since asking prices are what sellers hope for
    rather than what phones go for.

    Returns (euros, basis_label, n) or None when the sample is too thin -
    a benchmark built on three listings is worse than no benchmark,
    because it looks equally authoritative in the alert.
    """
    min_n = config.get("market_bench_min_n", 5)
    window = config.get("market_bench_window_days", 30)
    stats = benchmark(model, damaged=False, window_days=window, db_path=db_path)
    if stats["sold_median"] is not None and stats["n_sold"] >= 3:
        return stats["sold_median"], "verkocht", stats["n_sold"]
    asks = _resale_ask_pool(model, window, db_path)
    if len(asks) >= min_n:
        return median(asks) * ASK_TO_SALE_FACTOR, "vraag", len(asks)
    return None


def deal_score(
    model: Optional[str],
    ask_cents: int,
    config: dict,
    db_path: Path = storage.DB_PATH,
) -> Optional[dict]:
    """Score one listing's asking price against its model's resale value.

    Returns {"headroom_pct", "headroom_eur", "resale", "basis", "n",
    "tier"} or None when the price or the benchmark isn't usable.
    Never raises - a pricing hiccup must never cost an alert.
    """
    try:
        if not model or not ask_cents or ask_cents <= 0:
            return None
        ref = resale_reference(model, config, db_path)
        if ref is None:
            return None
        resale, basis, n = ref
        if resale <= 0:
            return None
        ask = ask_cents / 100
        headroom = resale - ask
        pct = headroom / resale
        tier = next(
            (label for threshold, label in DEAL_TIERS if pct >= threshold),
            DEAL_TIERS[-1][1],
        )
        return {
            "headroom_pct": pct, "headroom_eur": headroom,
            "resale": resale, "basis": basis, "n": n, "tier": tier,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("deal_score failed for %s: %s", model, exc)
        return None


def deal_line(
    model: Optional[str],
    ask_cents: int,
    price_type: str,
    config: dict,
    db_path: Path = storage.DB_PATH,
) -> str:
    """One 💸 alert line turning the price into a verdict, e.g.

        💸 🔥 TOPDEAL — vraagt €180, werkend ~€405 → €225 ruimte (56%)

    Bidding listings show the same numbers without a verdict: the "price"
    on those is a minimum bid, not what the phone will actually cost, so
    scoring it as if it were the asking price would be confidently wrong.
    Returns "" when there's nothing trustworthy to say.
    """
    score = deal_score(model, ask_cents, config, db_path)
    if score is None:
        return ""
    ask = ask_cents / 100
    room = score["headroom_eur"]
    # "-€62 ruimte", not "€-62 ruimte" - the sign belongs outside the
    # currency symbol, and these lines are read at a glance on a phone.
    room_text = f"-€{abs(room):.0f}" if room < 0 else f"€{room:.0f}"
    body = (
        f"vraagt €{ask:.0f}, werkend ~€{score['resale']:.0f} "
        f"({score['basis']}, n={score['n']}) → {room_text} "
        f"ruimte ({score['headroom_pct'] * 100:.0f}%)"
    )
    if price_type in ("MIN_BID", "FAST_BID"):
        # Openingsbod, not an asking price - no verdict, just the context.
        return f"💸 Bod vanaf {body}"
    return f"💸 {score['tier']} — {body}"


# --- Bargain sweep (working phones priced under market) ---------------------
#
# NEW 2026-07-23. The market_queries resale sweep ("iphone 15".."17") has
# been fetching WORKING phones every run since 07-11 and using them for
# statistics only - they were explicitly barred from ever alerting.
#
# But a working iPhone 15 at EUR 250 when the model sits at EUR 405 is a
# better flip than most damaged ones: no repair cost, no parts wait, no
# risk that the damage turns out to be a board fault. Those listings were
# already in memory, already parsed, already priced - and thrown away.
#
# This costs ZERO extra HTTP requests. It reuses the exact listings the
# sweep already pulled.
#
# Deliberately conservative, because a too-good-to-be-true price is the
# oldest scam signal there is: fixed-price only, hard price floor, an
# accessory-title veto, and a per-run cap so a benchmark glitch can never
# turn into a Telegram flood.

# Titles that are accessories/parts rather than a phone. Without this, a
# EUR 25 "hoesje voor iPhone 15" reads as a 94%-under-market iPhone 15.
_ACCESSORY_TITLE_TERMS = (
    "hoesje", "hoes", "case", "cover", "screenprotector", "screen protector",
    "protector", "oplader", "lader", "kabel", "adapter", "airpods",
    "onderdelen", "reparatie", "display los", "scherm los", "batterij los",
    "achterkant los", "lcd", "backcover", "doosje", "alleen doos", "lege doos",
    "simlock", "sticker", "skin", "glaasje", "camera lens beschermer",
)


def _looks_like_accessory(title: str) -> bool:
    return any(term in title.lower() for term in _ACCESSORY_TITLE_TERMS)


def mark_bargain_alerted(listing_id: str, db_path: Path = storage.DB_PATH) -> None:
    """Record that a bargain alert went out, so it only ever fires once.

    Deliberately tracked in market_listings, NOT in seen_listings: putting
    resale-sweep listings into the seen table would mark them processed
    for the damage pipeline too, so a phone that later gets relisted with
    damage wording would be silently skipped. That would be trading a new
    feature for a whole new class of miss.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE market_listings SET bargain_alerted_utc = ? WHERE listing_id = ?",
                (_now(), listing_id),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to mark bargain alert for %s: %s", listing_id, exc)


def _skip_as_already_alerted(listing_id: str, db_path: Path) -> bool:
    """True when this listing must NOT produce a (further) bargain alert.

    Note the deliberate asymmetry: a MISSING market_listings row also
    returns True. Dedup is an UPDATE against that row, so if it isn't
    there the mark is a no-op - and this scan runs every ~8 minutes, which
    would turn one un-dedupable listing into ~180 identical alerts a day.
    Ingest always writes the row before this runs (same model/listing_id
    conditions), so the miss case is defensive, and failing toward "no
    alert" is the only tolerable direction for a flood risk.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT bargain_alerted_utc FROM market_listings WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()
    if row is None:
        logger.warning(
            "Bargain candidate %s has no market_listings row - skipping, "
            "since the alert could not be de-duplicated", listing_id,
        )
        return True
    return bool(row[0])


def find_bargains(listings, config: dict, db_path: Path = storage.DB_PATH) -> list:
    """Working phones from the resale sweep priced well under their model's
    market value. Returns [(listing, score)] ordered best-first. Never raises."""
    if not config.get("bargain_alerts_enabled", True):
        return []
    try:
        return _find_bargains(listings, config, db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bargain sweep failed: %s", exc)
        return []


def _find_bargains(listings, config: dict, db_path: Path) -> list:
    min_headroom = config.get("bargain_min_headroom", 0.35)
    min_price = config.get("bargain_min_price_eur", 120)
    cap = config.get("bargain_max_alerts_per_run", 3)

    found = []
    for listing in listings:
        if not listing.listing_id or _looks_like_accessory(listing.title):
            continue
        # Bidding listings are excluded outright: an opening bid of EUR 1
        # on a EUR 400 phone would score as a 100% bargain every time.
        if listing.price_type not in ("FIXED", ""):
            continue
        if not listing.price_cents or listing.price_cents / 100 < min_price:
            continue
        if _is_damaged(listing, config):
            continue  # damaged phones already flow through the normal pipeline
        model = models.parse_model(listing.title)
        if not model:
            continue
        if _skip_as_already_alerted(listing.listing_id, db_path):
            continue
        score = deal_score(model, listing.price_cents, config, db_path)
        if score is None or score["headroom_pct"] < min_headroom:
            continue
        found.append((listing, score))

    found.sort(key=lambda pair: pair[1]["headroom_pct"], reverse=True)
    if len(found) > cap:
        logger.info(
            "Bargain sweep: %d candidates, capping at %d", len(found), cap
        )
    return found[:cap]


def bargain_line(score: dict, ask_cents: int) -> str:
    """The 🟢 [KOOPJE] line for a working-phone bargain alert."""
    return (
        f"🟢 [KOOPJE] werkend toestel — vraagt €{ask_cents / 100:.0f}, "
        f"markt ~€{score['resale']:.0f} ({score['basis']}, n={score['n']}) "
        f"→ €{score['headroom_eur']:.0f} onder markt "
        f"({score['headroom_pct'] * 100:.0f}%)"
    )


def benchmark_line(model: Optional[str], config: dict) -> str:
    """One [MARKT] alert line for a match, e.g.:

        📊 [MARKT] iphone 15 werkend: vraag mediaan €400 (n=55) · verkocht ~€375 (n=27)

    Werkend (repaired-resale) numbers, not schade: what he can SELL this
    phone for after repair is the number that decides the buy (Milad,
    2026-07-23 - same call he made for the weekly report on 07-18).

    Returns "" when there's no model or not enough data yet (n below
    market_bench_min_n) - alerts stay noise-free until stats mean something.
    Never raises."""
    try:
        if not model:
            return ""
        min_n = config.get("market_bench_min_n", 5)
        stats = benchmark(
            model, damaged=False,
            window_days=config.get("market_bench_window_days", 30),
        )
        parts = []
        if stats["ask_median"] is not None and stats["n_open"] >= min_n:
            parts.append(f"vraag mediaan €{stats['ask_median']:.0f} (n={stats['n_open']})")
        if stats["bid_median"] is not None and stats["n_bids"] >= 3:
            parts.append(f"bod mediaan €{stats['bid_median']:.0f} (n={stats['n_bids']})")
        if stats["sold_median"] is not None and stats["n_sold"] >= 3:
            parts.append(f"verkocht ~€{stats['sold_median']:.0f} (n={stats['n_sold']})")
        if not parts:
            return ""
        return f"📊 [MARKT] {model} werkend: " + " · ".join(parts)
    except Exception as exc:  # noqa: BLE001
        logger.error("benchmark_line failed for %s: %s", model, exc)
        return ""
