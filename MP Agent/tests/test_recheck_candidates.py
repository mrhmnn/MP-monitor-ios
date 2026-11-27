"""recheck_rejects.candidates() must see the rejections the CURRENT prompt writes.

2026-11-28 audit. The script shipped 2026-08-19, when an AI rejection still
read "AI review: <reason>". The no-defect gate landed the next afternoon and
rejections became "AI review (no clear target defect): <reason>" - so the
query stopped matching anything current, and `_SPECULATIVE_MARKERS` had no
entry for "no defect stated", which is how the current prompt words an
absence of evidence.

Measured on the live snapshot before the fix: --days 14 (the workflow's
default), 30 and 60 ALL returned zero candidates. After it, 232 at 14 days -
including "Apple iPhone 17 ... Screen already replaced but now has a crack
(re-repair possible)", a textbook target sitting rejected.

Both halves have to hold, so both are pinned here.
"""

import sqlite3

import pytest

import recheck_rejects


REJECT = "AI review (no clear target defect): "


def _db(tmp_path, rows):
    path = tmp_path / "seen.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE seen_listings (
            listing_id TEXT PRIMARY KEY, title TEXT, url TEXT,
            matched INTEGER, first_seen_utc TEXT, reason TEXT
        )
        """
    )
    for i, (matched, reason) in enumerate(rows):
        conn.execute(
            "INSERT INTO seen_listings VALUES (?,?,?,?,datetime('now','-2 days'),?)",
            (f"m{i}", f"iPhone 17 Pro #{i}", f"https://example.test/{i}", matched, reason),
        )
    conn.commit()
    conn.close()
    return str(path)


class TestCurrentRejectPrefixIsVisible:
    def test_no_clear_target_defect_prefix_is_a_candidate(self, tmp_path):
        db = _db(tmp_path, [(0, REJECT + "sealed new phone, no defect stated")])
        assert len(recheck_rejects.candidates(db, 14)) == 1

    def test_the_pre_gate_prefix_still_works(self, tmp_path):
        # Rows written before 2026-08-20 must not stop being visible.
        db = _db(tmp_path, [(0, "AI review: likely expensive board damage")])
        assert len(recheck_rejects.candidates(db, 14)) == 1

    def test_a_matched_listing_is_never_a_candidate(self, tmp_path):
        db = _db(tmp_path, [(1, "AI review: cracked back glass")])
        assert recheck_rejects.candidates(db, 14) == []

    def test_a_filter_rejection_is_not_a_candidate(self, tmp_path):
        # Only AI verdicts are re-testable; a filter reject never saw Haiku.
        db = _db(tmp_path, [(0, "hard exclude matched: 'waterschade'")])
        assert recheck_rejects.candidates(db, 14) == []


class TestSpeculativeMarkersMatchCurrentWording:
    @pytest.mark.parametrize(
        "reason",
        [
            "sealed new phone, no defect stated",
            "Working phone in very good condition, no defect stated; 89% battery",
            "phone in good condition, no defects stated.",
            "No defect described; sealed working phone offered for local pickup",
            "working phone, no defect to the device itself",
        ],
    )
    def test_absence_of_evidence_is_speculative(self, reason):
        assert recheck_rejects._is_speculative(REJECT + reason)

    @pytest.mark.parametrize(
        "reason",
        [
            "shop advertisement, not a phone for sale",
            "shop advertisement, not a phone listing for sale",
        ],
    )
    def test_shop_ads_are_not_re_tested(self, reason):
        """A positive identification, not a missing-evidence verdict. A looser
        prompt will never make a repair shop's own ad a buy, and ~120 of the
        last 30 days' rejections are these - re-testing them would spend the
        whole Haiku cap on the one bucket that cannot flip."""
        assert not recheck_rejects._is_speculative(REJECT + reason)

    def test_a_named_fault_is_not_re_tested(self):
        assert not recheck_rejects._is_speculative(REJECT + "iCloud locked device")
