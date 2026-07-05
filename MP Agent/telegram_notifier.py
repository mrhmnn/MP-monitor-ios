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

TELEGRAM_SEND_MESSAGE = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_SEND_PHOTO = "https://api.telegram.org/bot{token}/sendPhoto"


def _get_credentials():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat_id


def send_message(text: str) -> bool:
    """Plain text message - used when no image is available."""
    token, chat_id = _get_credentials()

    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set - can't send notification. "
            "Message that would have been sent:\n%s",
            text,
        )
        return False

    url = TELEGRAM_SEND_MESSAGE.format(token=token)
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
        logger.error("Failed to send Telegram text message: %s", exc)
        return False


def send_photo(image_url: str, caption: str) -> bool:
    """
    Send a photo directly via the sendPhoto API - Telegram fetches the
    image URL itself and delivers it as a proper inline photo message.
    This is more reliable than relying on Telegram's auto-preview of the
    listing URL, which can silently fail on Marktplaats' JS-heavy pages.

    Caption has a 1024-character Telegram limit - our messages are well
    below that in practice, but we truncate defensively just in case.
    """
    token, chat_id = _get_credentials()

    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set - can't send notification. "
            "Photo caption that would have been sent:\n%s",
            caption,
        )
        return False

    url = TELEGRAM_SEND_PHOTO.format(token=token)
    payload = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": caption[:1024],
        "parse_mode": "Markdown",
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        # If sendPhoto fails (e.g. Telegram couldn't fetch the image),
        # fall back to a plain text message so the notification isn't
        # lost entirely.
        logger.warning(
            "sendPhoto failed (%s), falling back to plain text message", exc
        )
        return send_message(caption)


def send_listing(image_url: str, caption: str) -> bool:
    """
    Dispatch to sendPhoto if we have an image URL, otherwise sendMessage.
    This is the entry point main.py calls.
    """
    if image_url:
        return send_photo(image_url, caption)
    return send_message(caption)


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
        # Note: Marktplaats' individual listing pages show a "Sinds <date>"
        # field (e.g. "Sinds 8 jun '26") which is the true original posting
        # date - not the bump/repost date shown in search results.
        lines.append(f"🗓️ {posted_date}")

    if distance_km is not None and duration_minutes is not None:
        lines.append(f"📍 {distance_km:.0f} km · 🚗 ~{duration_minutes} min from Veenendaal")
    else:
        lines.append("📍 distance unavailable")

    lines.append(f"🔧 {match_reason}")
    lines.append(f"[Open listing]({url})")

    return "\n".join(lines)
