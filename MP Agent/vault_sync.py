"""
vault_sync.py

Local bridge between the GitHub-Actions scraper (state lives on the repo's
data branch) and the 2ND Brain Obsidian vault on this PC: pulls the latest
seen_listings.db snapshot from origin/data and updates the vault note
"(C) Market Prices.md" via pricebench.write_markdown (snapshot
section replaced in place + one dated history row per model appended,
deduped per day - the history table becomes the price-trend asset).

Meant to run on this machine via Windows Task Scheduler (daily), or
manually: python vault_sync.py
"""

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vault_sync")

REPO_DIR = Path(__file__).resolve().parent.parent
VAULT_NOTE = Path(
    r"C:\Users\milad\Downloads\2ND Brain\2ND BRAIN\01 Projects"
    r"\Marktplaats Scraper iPhones\(C) Market Prices.md"
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


def main() -> None:
    # Point storage at the snapshot BEFORE importing market/pricebench:
    # market.benchmark's db_path default binds storage.DB_PATH at import.
    storage.DB_PATH = fetch_latest_db()
    import pricebench  # noqa: PLC0415

    models = pricebench.tracked_models(WINDOW_DAYS)
    if not models:
        logger.warning("No tracked models yet - nothing to write")
        return
    lines = [f"📊 Marktplaats prijzen — laatste {WINDOW_DAYS} dagen"]
    for model in models:
        lines.extend(pricebench.report_model(model, None, WINDOW_DAYS))
    pricebench.write_markdown(str(VAULT_NOTE), "\n".join(lines), models, WINDOW_DAYS)


if __name__ == "__main__":
    sys.exit(main())
