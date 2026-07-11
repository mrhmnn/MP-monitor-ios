"""
vault_sync.py

Writes the current market-price benchmark into the Obsidian vault as
"(C) Marktplaats Prijzen.md" - the local bridge between the GitHub-Actions
scraper (state lives on the repo's data branch) and the 2ND Brain vault
on this PC.

Flow: fetch the latest seen_listings.db snapshot from origin/data into a
temp file, run market.benchmark() over it, render one markdown note, and
overwrite the vault note in place. Read-only towards the repo; the note
is fully regenerated each run (it's a (C) Claude-generated file, never
hand-edited).

Meant to run on this machine via Windows Task Scheduler (daily), or
manually: python vault_sync.py
"""

import logging
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vault_sync")

REPO_DIR = Path(__file__).resolve().parent.parent
VAULT_NOTE = Path(
    r"C:\Users\milad\Downloads\2ND Brain\2ND BRAIN\01 Projects"
    r"\Marktplaats Scraper iPhones\(C) Marktplaats Prijzen.md"
)
WINDOW_DAYS = 30


def fetch_latest_db() -> Path:
    """Pull the freshest DB snapshot from origin/data into a temp file.
    Falls back to the local working-copy DB if the fetch fails (offline)."""
    tmp = Path(tempfile.gettempdir()) / "mp_vault_sync.db"
    try:
        subprocess.run(
            ["git", "fetch", "origin", "data", "-q"],
            cwd=REPO_DIR, check=True, timeout=60,
        )
        blob = subprocess.run(
            ["git", "show", "origin/data:MP Agent/seen_listings.db"],
            cwd=REPO_DIR, check=True, timeout=60, capture_output=True,
        ).stdout
        tmp.write_bytes(blob)
        logger.info("Fetched DB snapshot from origin/data (%d KB)", len(blob) // 1024)
        return tmp
    except Exception as exc:  # noqa: BLE001
        local = Path(__file__).parent / "seen_listings.db"
        if local.exists():
            logger.warning("Couldn't fetch data branch (%s), using local DB", exc)
            return local
        raise SystemExit(f"No database available: {exc}") from exc


def _fmt(value) -> str:
    return f"€{value:.0f}" if value is not None else "–"


def render_note(db_path: Path) -> str:
    # Import market AFTER pointing storage at the snapshot, so its
    # db_path defaults bind to the right file.
    storage.DB_PATH = db_path
    import market  # noqa: PLC0415

    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    with sqlite3.connect(db_path) as conn:
        models = [r[0] for r in conn.execute(
            "SELECT DISTINCT model FROM market_listings WHERE last_seen_utc >= ? ORDER BY model",
            (cutoff,),
        )]
        recent_sales = conn.execute(
            """
            SELECT model, storage_gb, title, final_ask_cents, final_bid_cents, closed_utc, url
            FROM market_listings
            WHERE status = 'gone' AND bid_count > 0
              AND julianday(closed_utc) - julianday(first_seen_utc) <= ?
            ORDER BY closed_utc DESC LIMIT 15
            """,
            (market.SALE_MAX_DAYS,),
        ).fetchall()
        total, obs = conn.execute(
            "SELECT (SELECT COUNT(*) FROM market_listings), (SELECT COUNT(*) FROM price_obs)"
        ).fetchone()

    now_nl = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---",
        "tags: [marktplaats, prijzen, auto-generated]",
        f"updated: {now_nl}",
        "---",
        "",
        "# (C) Marktplaats Prijzen",
        "",
        f"> Auto-generated door `vault_sync.py` — laatste {WINDOW_DAYS} dagen, "
        f"{total} listings getrackt, {obs} prijsobservaties. NIET handmatig bewerken.",
        "",
        "**Leeswijzer:** *vraag* = mediaan vraagprijs van open listings (optimistisch); "
        "*bod* = mediaan hoogste bod; *verkocht* = listings die binnen 7 dagen verdwenen "
        "MET biedingen — de beste proxy voor echte verkoopprijzen. "
        "Verkocht-stats hebben ~2-4 weken data nodig.",
        "",
        "## Benchmark per model",
        "",
        "| Model | Kant | Vraag (p25–p75) | n | Bod | n | Verkocht | n | Verlopen |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for model in models:
        for label, damaged in (("schade", True), ("werkend", False)):
            s = market.benchmark(model, damaged=damaged, window_days=WINDOW_DAYS, db_path=db_path)
            if s["n_open"] == 0 and s["n_sold"] == 0 and s["n_expired"] == 0:
                continue
            spread = (
                f" ({_fmt(s['ask_p25'])}–{_fmt(s['ask_p75'])})"
                if s["ask_p25"] is not None else ""
            )
            lines.append(
                f"| {model} | {label} | {_fmt(s['ask_median'])}{spread} | {s['n_open']} "
                f"| {_fmt(s['bid_median'])} | {s['n_bids']} "
                f"| {_fmt(s['sold_median'])} | {s['n_sold']} | {s['n_expired']} |"
            )

    lines += ["", "## Recent verkocht (proxy: weg binnen 7 dagen mét biedingen)", ""]
    if recent_sales:
        lines += ["| Datum | Model | Titel | Laatste vraag | Hoogste bod |", "|---|---|---|---|---|"]
        for model, gb, title, ask, bid, closed, url in recent_sales:
            date = (closed or "")[:10]
            gb_txt = f" {gb}GB" if gb else ""
            title_short = title[:45].replace("|", "/")
            lines.append(
                f"| {date} | {model}{gb_txt} | [{title_short}]({url}) "
                f"| {_fmt(ask / 100 if ask else None)} | {_fmt(bid / 100 if bid else None)} |"
            )
    else:
        lines.append("*Nog geen verkocht-data — de tracker draait sinds 11 juli 2026.*")

    lines += [
        "",
        "---",
        "Gerelateerd: [[(C) Flips Ledger]] · [[(C) Foneday Parts Prices]]",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    db_path = fetch_latest_db()
    note = render_note(db_path)
    VAULT_NOTE.parent.mkdir(parents=True, exist_ok=True)
    VAULT_NOTE.write_text(note, encoding="utf-8")
    logger.info("Vault note updated: %s", VAULT_NOTE)


if __name__ == "__main__":
    sys.exit(main())
