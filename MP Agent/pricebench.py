"""
pricebench.py

CLI report over the market-price data collected by market.py.

Usage:
    python pricebench.py "iphone 14"              # one model, all storage
    python pricebench.py "iphone 14" --storage 128
    python pricebench.py --summary                # all tracked models
    python pricebench.py --summary --telegram     # + send to Telegram
    python pricebench.py --days 14                # narrower window
    python pricebench.py --summary --markdown "path/to/(C) Market Prices.md"
                                                  # + update Obsidian note

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


# --- Obsidian note output -----------------------------------------------

_SNAPSHOT_START = "<!-- snapshot:start -->"
_SNAPSHOT_END = "<!-- snapshot:end -->"

_NOTE_TEMPLATE = """# (C) Market Prices — Marktplaats iPhones

Auto-updated by `pricebench.py --markdown` (weekly review step). Do not
edit the snapshot section by hand — it gets replaced on every run.

**Hoe te lezen:** *vraag* = mediaan vraagprijs van open listings
(optimistisch). *bod* = mediaan hoogste bod. *verkocht* = listings die
binnen 7 dagen verdwenen MET biedingen — de beste proxy voor echte
verkoopprijzen die Marktplaats biedt.

## Laatste snapshot

{start}
{snapshot}
{end}

## Geschiedenis

Eén rij per model per week — dit wordt op termijn de echte asset:
prijstrends zien vóór je koopt.

| Datum | Model | Schade vraag | Schade verkocht (n) | Werkend vraag | Werkend verkocht (n) |
|---|---|---|---|---|---|
"""


def _history_rows(models: list[str], window_days: int) -> list[str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    for model in models:
        damaged = market.benchmark(model, damaged=True, window_days=window_days)
        working = market.benchmark(model, damaged=False, window_days=window_days)
        rows.append(
            f"| {today} | {model} "
            f"| {_fmt(damaged['ask_median'])} "
            f"| {_fmt(damaged['sold_median'])} ({damaged['n_sold']}) "
            f"| {_fmt(working['ask_median'])} "
            f"| {_fmt(working['sold_median'])} ({working['n_sold']}) |"
        )
    return rows


def write_markdown(path: str, report_text: str, models: list[str], window_days: int) -> None:
    """Update the vault note: replace the snapshot section, append one
    history row per model. Creates the note from the template if absent."""
    from pathlib import Path

    note = Path(path)
    snapshot = f"```\n{report_text}\n```"
    if note.exists():
        content = note.read_text(encoding="utf-8")
        start = content.find(_SNAPSHOT_START)
        end = content.find(_SNAPSHOT_END)
        if start != -1 and end != -1:
            content = (
                content[: start + len(_SNAPSHOT_START)]
                + "\n" + snapshot + "\n"
                + content[end:]
            )
        else:
            content += f"\n\n## Laatste snapshot\n\n{_SNAPSHOT_START}\n{snapshot}\n{_SNAPSHOT_END}\n"
    else:
        note.parent.mkdir(parents=True, exist_ok=True)
        content = _NOTE_TEMPLATE.format(
            start=_SNAPSHOT_START, snapshot=snapshot, end=_SNAPSHOT_END
        )
    if not content.endswith("\n"):
        content += "\n"
    # Skip rows already present for today's date+model, so rerunning the
    # weekly step doesn't duplicate history.
    new_rows = [
        row for row in _history_rows(models, window_days)
        if row.split("|")[1].strip() + "|" + row.split("|")[2]
        not in {line.split("|")[1].strip() + "|" + line.split("|")[2]
                for line in content.splitlines() if line.startswith("| 2")}
    ]
    if new_rows:
        content += "\n".join(new_rows) + "\n"
    note.write_text(content, encoding="utf-8")
    print(f"\nObsidian note updated: {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Marktplaats iPhone price benchmark")
    parser.add_argument("model", nargs="?", help='e.g. "iphone 14" or "iphone 15 pro max"')
    parser.add_argument("--storage", type=int, help="storage in GB, e.g. 128")
    parser.add_argument("--days", type=int, default=30, help="lookback window (default 30)")
    parser.add_argument("--summary", action="store_true", help="report every tracked model")
    parser.add_argument("--telegram", action="store_true", help="also send the report to Telegram")
    parser.add_argument("--markdown", metavar="PATH",
                        help="update an Obsidian note: replace its snapshot section and append history rows")
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
        models = [args.model.lower().strip()]
        lines.extend(report_model(models[0], args.storage, args.days))

    text = "\n".join(lines)
    print(text)

    if args.markdown:
        write_markdown(args.markdown, text, models, args.days)

    if args.telegram:
        import html

        import telegram_notifier

        # Telegram rejects messages over 4096 chars with a 400 - and a
        # 15-model summary flirts with that limit. Split on model blocks
        # (every model section starts with a blank line) so each chunk
        # stays a valid, readable <pre> message on its own.
        chunks: list[str] = []
        current = ""
        for block in text.split("\n\n"):
            candidate = f"{current}\n\n{block}" if current else block
            if len(candidate) > 3500 and current:
                chunks.append(current)
                current = block
            else:
                current = candidate
        if current:
            chunks.append(current)

        results = [
            telegram_notifier.send_message(f"<pre>{html.escape(chunk)}</pre>")
            for chunk in chunks
        ]
        sent = all(results)
        print(f"\nTelegram: {'sent' if sent else 'FAILED'} ({len(chunks)} message(s))")


if __name__ == "__main__":
    load_dotenv()
    main()
