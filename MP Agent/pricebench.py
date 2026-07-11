"""
pricebench.py

CLI report over the market-price data collected by market.py.

Usage:
    python pricebench.py "iphone 14"              # one model, all storage
    python pricebench.py "iphone 14" --storage 128
    python pricebench.py --summary                # all tracked models
    python pricebench.py --summary --telegram     # + send to Telegram
    python pricebench.py --days 14                # narrower window

"verkocht" = listings gone within 7 days that had at least one bid - the
closest thing Marktplaats offers to a real sold price. "vraag" medians are
asking prices of currently-open listings (optimistic by nature). Sold
stats need ~2-4 weeks of scan data before n gets meaningful.
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import market
import storage


def _fmt(value, prefix="€"):
    return f"{prefix}{value:.0f}" if value is not None else "-"


def _segment_line(label: str, stats: dict) -> str:
    ask = _fmt(stats["ask_median"])
    spread = ""
    if stats["ask_p25"] is not None:
        spread = f" ({_fmt(stats['ask_p25'])}-{_fmt(stats['ask_p75'])})"
    return (
        f"  {label:<9} vraag {ask}{spread} n={stats['n_open']} | "
        f"bod {_fmt(stats['bid_median'])} n={stats['n_bids']} | "
        f"verkocht {_fmt(stats['sold_median'])} n={stats['n_sold']} "
        f"(verlopen: {stats['n_expired']})"
    )


def report_model(model: str, storage_gb, window_days: int) -> list[str]:
    lines = [f"\n{model.upper()}" + (f" {storage_gb}GB" if storage_gb else "")]
    for label, damaged in (("schade", True), ("werkend", False)):
        stats = market.benchmark(model, storage_gb=storage_gb, damaged=damaged,
                                 window_days=window_days)
        lines.append(_segment_line(label, stats))
    return lines


def tracked_models(window_days: int) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    with sqlite3.connect(storage.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT model FROM market_listings WHERE last_seen_utc >= ? ORDER BY model",
            (cutoff,),
        ).fetchall()
    return [r[0] for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Marktplaats iPhone price benchmark")
    parser.add_argument("model", nargs="?", help='e.g. "iphone 14" or "iphone 15 pro max"')
    parser.add_argument("--storage", type=int, help="storage in GB, e.g. 128")
    parser.add_argument("--days", type=int, default=30, help="lookback window (default 30)")
    parser.add_argument("--summary", action="store_true", help="report every tracked model")
    parser.add_argument("--telegram", action="store_true", help="also send the report to Telegram")
    args = parser.parse_args()

    storage.init_db()
    lines = [f"📊 Marktplaats prijzen — laatste {args.days} dagen"]

    if args.summary or not args.model:
        models = tracked_models(args.days)
        if not models:
            lines.append("Nog geen data verzameld - laat de scanner een paar dagen draaien.")
        for model in models:
            lines.extend(report_model(model, None, args.days))
    else:
        lines.extend(report_model(args.model.lower().strip(), args.storage, args.days))

    text = "\n".join(lines)
    print(text)

    if args.telegram:
        import html

        import telegram_notifier
        sent = telegram_notifier.send_message(f"<pre>{html.escape(text)}</pre>")
        print(f"\nTelegram: {'sent' if sent else 'FAILED'}")


if __name__ == "__main__":
    load_dotenv()
    main()
