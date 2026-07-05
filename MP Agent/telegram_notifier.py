"""
telegram_notifier.py

Sends notifications to your phone via a Telegram bot. Plain HTTP calls to
the Bot API - no need for a heavier library for something this simple.

Setup (one-time):
  1. Message @BotFather on Telegram, send /newbot, follow the prompts.
     You'll get a bot token - put it in .env as TELEGRAM_BOT_TOKEN.
  2. Send any message to your new bot (so it knows who you are).
  3. Visit https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates in a browser
     and find your numeric "chat" -> "id" field - put it in .env as
     TELEGRAM_CHAT_ID.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set - can't send notification. "
            "Message that would have been sent:\n%s",
            text,
        )
        return False

    url = TELEGRAM_API_BASE.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send Telegram message: %s", exc)
        return False


def format_listing_message(
    title: str,
    price_text: str,
    url: str,
    match_reason: str,
    distance_km: float | None,
    duration_minutes: int | None,
    posted_date: str = "",
) -> str:
    lines = [f"*{title}*", f"💰 {price_text or 'Bieden'}"]

    if posted_date:
        # Note: Marktplaats only provides day-level precision (e.g.
        # "Vandaag", "Gisteren", or a date) - never an exact time.
        lines.append(f"🗓️ {posted_date}")

    if distance_km is not None and duration_minutes is not None:
        lines.append(f"📍 {distance_km:.0f} km · 🚗 ~{duration_minutes} min from Veenendaal")
    else:
        lines.append("📍 distance unavailable")

    lines.append(f"🔧 {match_reason}")
    lines.append(f"[Open listing]({url})")

    return "\n".join(lines)
