"""Unit tests for model-name parsing and market helpers."""

import pytest

import market
import models


class TestParseModel:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("iPhone 15 Pro Max 256GB kapot scherm", "iphone 15 pro max"),
            ("iphone15 promax schade", "iphone 15 pro max"),
            ("iPhone 14 Plus laadt niet op", "iphone 14 plus"),
            ("iPhone 16 - lichte schade", "iphone 16"),
            ("IPH 14 Pro Max scherm kapot", "iphone 14 pro max"),  # 2026-07-13 fix
            ("iph16 achterkant kapot", "iphone 16"),
            ("Samsung S24 scherm kapot", None),
            ("iPhone 16e nieuw", None),   # e-models not tracked
            ("iPhone 13 scherm kapot", None),  # below target range
        ],
    )
    def test_parse(self, title, expected):
        assert models.parse_model(title) == expected


class TestParseStorage:
    @pytest.mark.parametrize(
        ("storage_text", "title", "expected"),
        [
            ("128 GB", "", 128),
            ("", "iPhone 15 256GB kapot", 256),
            ("1 TB", "", 1024),
            ("", "iPhone 15 kapot", None),
        ],
    )
    def test_parse(self, storage_text, title, expected):
        assert market.parse_storage_gb(storage_text, title) == expected
