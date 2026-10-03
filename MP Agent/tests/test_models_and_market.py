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


class TestVariantNotImmediatelyAfterTheNumber:
    """The variant used to have to sit directly after the generation number,
    so three real title shapes lost their price verdict (2026-10-03 audit:
    178 market rows, 15 alerts). The worst case was
    "iPhone15Pro128GB Gebarsten achterkant", which alerted on 09-25, 09-26,
    09-28 and 10-02 parsing as None - no deal line, no [MARKT] line, four
    times over.
    """

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # Reversed word order - 11 rows in live history.
            ("Iphone 15 Max Pro 256GB Grey (achterkant barst)", "iphone 15 pro max"),
            ("Iphone 14 max pro", "iphone 14 pro max"),
            ("iPhone 16 Max Pro 256GB", "iphone 16 pro max"),
            # Storage/separator noise between number and variant.
            ("IPHONE 17/256 PRO MAX NIEUW GESEALD", "iphone 17 pro max"),
            ("IPHONE 18/256 PRO BLACK NIEUW", "iphone 18 pro"),
            # Variant glued to the storage size.
            ("iPhone15Pro128GB Gebarsten achterkant", "iphone 15 pro"),
            ("Iphone 16 pro max256gb", "iphone 16 pro max"),
            ("Apple iPhone 16 Pro256GB - kleine barst links boven", "iphone 16 pro"),
            ("Iphone 15 plus128GB", "iphone 15 plus"),
            # Generation glued to a following word.
            ("iPhone 15met 128GB, gebarsten achterkant", "iphone 15"),
        ],
    )
    def test_now_parses(self, title, expected):
        assert models.parse_model(title) == expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            # The gap may never cross a LETTER, or a damage word becomes a
            # variant. These are the regressions the widening could have
            # caused, and must not.
            ("iPhone 15, problemen met scherm", "iphone 15"),
            ("iPhone 16 plusminus nieuw", "iphone 16"),
            ("iPhone 15 128GB Groen", "iphone 15"),
            ("Iphone 18 zwart pro 512 gb geen bon", "iphone 18"),
            # A variant belonging to a DIFFERENT phone in a list stays out.
            ("iPhone 17 / 17 Pro / 17 Pro Max ALLES MOET WEG", "iphone 17"),
            ("Kapotte Iphone 15 &16 pro", "iphone 15"),
            ("Screenprotector iPhone 14/13/13 Pro Panzer Glass", "iphone 14"),
            # The e-model exclusion survives losing the old trailing guard.
            ("iPhone 16e", None),
            ("iPhone 17e (2022) 64GB Wit - Achterkant gebarsten", None),
            ("iPhone 16e 128GB 92% batterij werkt volledig", None),
            # Not an iPhone 15.
            ("iPhone 150- Gebruikt met schade", None),
        ],
    )
    def test_still_rejected_or_base(self, title, expected):
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
