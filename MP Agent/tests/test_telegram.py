"""Unit tests for Telegram message formatting (no network)."""

import telegram_notifier


class TestFormatListingMessage:
    def test_html_escapes_user_text(self):
        msg = telegram_notifier.format_listing_message(
            title="iPhone 14 *NIEUW* <kapot> & meer",
            price_text="€100",
            url="https://www.marktplaats.nl/v/x/m123-test",
            match_reason="primary keyword matched: 'scherm kapot'",
            distance_km=42.0,
            duration_minutes=35,
        )
        assert "<kapot>" not in msg
        assert "&lt;kapot&gt;" in msg
        assert "&amp;" in msg

    def test_reserved_flag_adds_warning_line(self):
        msg = telegram_notifier.format_listing_message(
            title="iPhone 15",
            price_text="Bieden",
            url="https://example.com",
            match_reason="x",
            distance_km=None,
            duration_minutes=None,
            is_reserved=True,
        )
        assert "Gereserveerd" in msg

    def test_no_reserved_line_by_default(self):
        msg = telegram_notifier.format_listing_message(
            title="iPhone 15",
            price_text="Bieden",
            url="https://example.com",
            match_reason="x",
            distance_km=None,
            duration_minutes=None,
        )
        assert "Gereserveerd" not in msg

    def test_city_shown_when_distance_unknown(self):
        msg = telegram_notifier.format_listing_message(
            title="iPhone 15",
            price_text="Bieden",
            url="https://example.com",
            match_reason="x",
            distance_km=None,
            duration_minutes=None,
            city="Roosendaal",
        )
        assert "Roosendaal" in msg
        assert "distance unavailable" in msg
