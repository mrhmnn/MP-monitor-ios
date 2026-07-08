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

import html
import logging
import os

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_SEND_MESSAGE = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_SEND_PHOTO = "https://api.telegram.org/bot{token}/sendPhoto"

# HTML parse mode, not Markdown. Telegram's legacy Markdown mode makes the
# API reject the ENTIRE message with a 400 if the text contains an unpaired
# *, _, [ or ` - and listing titles are user-written text that regularly
# contains exactly those characters ("iPhone 14 *NIEUW*", "LET OP_..."),
# which meant a silently lost alert. With HTML mode we control the only
# markup ourselves and html.escape() the user-written parts.


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
        "parse_mode": "HTML",
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
        "parse_mode": "HTML",
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
    profit_est=None,  # profit.ProfitEstimate or None
) -> str:
    # User-written text (title, reason, price, date) gets escaped so it can
    # never break the HTML parse mode - see note at the top of this module.
    lines = [f"<b>{html.escape(title)}</b>", f"💰 {html.escape(price_text or 'Bieden')}"]

    if posted_date:
        # Note: Marktplaats' individual listing pages show a "Sinds <date>"
        # field (e.g. "Sinds 8 jun '26") which is the true original posting
        # date - not the bump/repost date shown in search results.
        lines.append(f"🗓️ {html.escape(posted_date)}")

    if distance_km is not None and duration_minutes is not None:
        lines.append(f"📍 {distance_km:.0f} km · 🚗 ~{duration_minutes} min from Veenendaal")
    else:
        lines.append("📍 distance unavailable")

    lines.append(f"🔧 {html.escape(match_reason)}")
    lines.extend(_format_profit_lines(profit_est))
    lines.append(f'<a href="{html.escape(url, quote=True)}">Open listing</a>')

    return "\n".join(lines)


def _format_profit_lines(est) -> list[str]:
    """Phase 2 profit block. All numeric fields come from our own committed
    data files (profit.py), so nothing here is user-written - but model
    names are still escaped out of habit. Degrades line by line: whatever
    number couldn't be determined is simply left out.

    Example output:
        💶 Swappie betaalt [SWAPPIE]: €210 (Goed) / €195 (Matig) · 128GB
        🔩 Repair est. [FONEDAY]: €29.95 (screen)
        📈 Profit after repair: €55 (Goed) / €40 (Matig)

    "Swappie betaalt" is the trade-in payout (verkoop flow) for a fully
    working phone - the guaranteed exit after repair - not their much
    higher retail price.
    """
    if est is None or est.model is None:
        return []

    lines = []

    if est.swappie_fair is not None:
        from profit import GRADE_LABELS  # local import avoids a cycle at module load

        good_label = GRADE_LABELS.get(est.swappie_good_grade, est.swappie_good_grade)
        fair_label = GRADE_LABELS.get(est.swappie_fair_grade, est.swappie_fair_grade)
        parts = []
        if est.swappie_good is not None and est.swappie_good_grade != est.swappie_fair_grade:
            parts.append(f"€{est.swappie_good:.0f} ({html.escape(good_label)})")
        parts.append(f"€{est.swappie_fair:.0f} ({html.escape(fair_label)})")
        storage = f" · {est.storage_gb}GB" if est.storage_gb else ""
        if est.storage_assumed and storage:
            storage += " (aanname)"
        lines.append(f"💶 Swappie betaalt [SWAPPIE]: {' / '.join(parts)}{storage}")

    if est.repair_cost is not None:
        parts_text = ", ".join(est.repair_parts)
        if est.repair_assumed:
            parts_text += " (aanname)"
        lines.append(f"🔩 Repair est. [FONEDAY]: €{est.repair_cost:.2f} ({parts_text})")

    if est.profit_fair is not None:
        prefix = "📈" if est.profit_fair > 0 else "📉"
        profit = f"{prefix} Profit after repair: "
        if est.profit_good is not None and est.profit_good != est.profit_fair:
            profit += f"€{est.profit_good:.0f} (Goed) / €{est.profit_fair:.0f} (Matig)"
        else:
            profit += f"€{est.profit_fair:.0f}"
        if est.asking_is_bid_floor:
            # Asking price was a "Bieden vanaf" floor - the real purchase
            # price will be higher, so this profit number is a ceiling.
            profit += " (bij minimumbod)"
        lines.append(profit)
    elif est.break_even is not None:
        # No asking price on the listing ("Bieden") - show the max sane
        # bid instead: pay more than this and the flip is a loss even at
        # Swappie's Matig trade-in payout.
        lines.append(f"📈 Break-even bod (max): €{est.break_even:.0f}")

    return lines
