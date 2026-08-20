"""The no-defect gate: undamaged phones must not reach Telegram (2026-08-20).

For one day the AI verdict was context-only and every listing that reached AI
review alerted. What that produced, straight from the alert log of 08-20:

    "sealed refurbished phone, no defect stated"
    "used phone in near-new condition, no defect stated"
    "Shop advertisement for a working used phone with no defect"
    "Surface scratches on intact screen, normal wear"
    "phone case accessory only, not a phone"

Milad: "used products giving me alerts". So the verdict gates again - but only
on the question the keyword filter genuinely cannot answer ("is there a defect
at all"), never on how expensive the repair looks. Everything that passes still
carries the AI's damage sentence into the alert.

These run the real scan cycle with the network, Telegram, geocoding and the
price tracker stubbed out, so they pin the behaviour end to end rather than
re-testing a boolean.
"""

from pathlib import Path

import pytest
import yaml

import ai_classifier
import distance
import filters
import main
import market
import scraper
import storage
import telegram_notifier


def _isolate_db(path, monkeypatch):
    """Point every storage function at a throwaway DB.

    storage's functions take `db_path: Path = DB_PATH`, and that default was
    bound when the module was imported - so patching storage.DB_PATH alone
    leaves every call still writing to the real seen_listings.db. Rebinding
    the defaults is what actually isolates the run.
    """
    import inspect

    real = storage.DB_PATH
    monkeypatch.setattr(storage, "DB_PATH", path)
    for name in dir(storage):
        func = getattr(storage, name)
        if not inspect.isfunction(func) or not func.__defaults__:
            continue
        if real in func.__defaults__:
            monkeypatch.setattr(
                func,
                "__defaults__",
                tuple(path if d == real else d for d in func.__defaults__),
            )
    storage.init_db(path)


def _row(listing_id: str):
    """(reason, outcome) - neither is exposed by storage.get_seen_record()."""
    import sqlite3

    with sqlite3.connect(storage.DB_PATH) as conn:
        return conn.execute(
            "SELECT reason, outcome FROM seen_listings WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()


def _listing(listing_id="m1", title="iPhone 15 Pro", description="", **kwargs):
    return scraper.Listing(
        listing_id=listing_id,
        title=title,
        description_snippet=description,
        price_text="€ 400,00",
        location_text="Veenendaal",
        url=f"https://www.marktplaats.nl/v/{listing_id}",
        price_cents=40000,
        **kwargs,
    )


@pytest.fixture
def scan(tmp_path, monkeypatch):
    """One search query, no network, capture what would have been sent."""
    _isolate_db(tmp_path / "test.db", monkeypatch)

    config = yaml.safe_load(
        (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
    )
    config["search_queries"] = ["iphone schade"]
    config["market_queries"] = []
    config["request_delay_seconds"] = 0

    state = {"listings": [], "verdict": None, "sent": [], "config": config}

    monkeypatch.setattr(scraper, "fetch_listings", lambda *a, **kw: state["listings"])
    monkeypatch.setattr(
        scraper, "fetch_listing_details", lambda *a, **kw: scraper.ListingDetails()
    )
    monkeypatch.setattr(
        ai_classifier, "classify_ambiguous_listing", lambda *a, **kw: state["verdict"]
    )
    monkeypatch.setattr(
        telegram_notifier,
        "send_listing",
        lambda image, message, **kw: state["sent"].append(message) or True,
    )
    monkeypatch.setattr(telegram_notifier, "send_message", lambda *a, **kw: True)
    monkeypatch.setattr(
        distance,
        "get_driving_distance_from_coords",
        lambda *a, **kw: distance.DistanceResult(10, 12, "driving", "OK"),
    )
    monkeypatch.setattr(
        distance,
        "get_driving_distance",
        lambda *a, **kw: distance.DistanceResult(10, 12, "driving", "OK"),
    )
    for name in ("ingest_listings", "poll_bids", "check_closures"):
        monkeypatch.setattr(market, name, lambda *a, **kw: None)
    monkeypatch.setattr(market, "benchmark_line", lambda *a, **kw: "")
    monkeypatch.setattr(market, "deal_line", lambda *a, **kw: "")

    def run(listing, verdict):
        state["listings"] = [listing]
        state["verdict"] = verdict
        main.run_scan_cycle(config)
        return state["sent"]

    state["run"] = run
    return state


def test_a_working_used_phone_does_not_alert(scan):
    sent = scan["run"](
        _listing(title="Apple iPhone 16 Pro - 128GB - Zwart", description="Nette staat, schade? geen."),
        ai_classifier.AiVerdict(False, "sealed refurbished phone, no defect stated"),
    )
    assert sent == []
    assert not storage.get_seen_record("m1")["matched"]
    # The verdict is kept verbatim, so why-no-alert forensics stay a one-liner.
    assert "no clear target defect" in _row("m1")[0]


def test_a_damaged_phone_still_alerts_with_the_ai_sentence(scan):
    sent = scan["run"](
        _listing(title="iPhone 15 Pro met schade", description="Zie foto's voor de staat."),
        ai_classifier.AiVerdict(True, "Cracked back glass; phone otherwise functional."),
    )
    assert len(sent) == 1
    assert "Cracked back glass" in sent[0]
    assert _row("m1")[1] == "alerted"


def test_parts_framing_is_not_a_no_defect_listing(scan):
    """"voor onderdelen" is a buy signal, and the gate must never eat it.

    Measured over DB history: 39 parts-framed listings reached AI review and
    not one came back "no clear target defect" - the prompt treats the phrase
    as damage ASSERTED but unnamed, which is relevant by rule. This pins the
    end of that chain: the verdict it produces still alerts.
    """
    sent = scan["run"](
        _listing(title="Iphone 15 voor onderdelen", description="Zie foto's."),
        ai_classifier.AiVerdict(
            True, "Damage asserted via 'voor onderdelen' with no deep fault named."
        ),
    )
    assert len(sent) == 1


def test_a_failed_ai_call_is_retried_not_buried(scan):
    """An API error is not a verdict.

    This mattered less while everything alerted regardless; with the gate on,
    a 529 that got recorded as "not relevant" would bury the listing forever.
    It must stay unseen so the next run (~8 min later) tries again.
    """
    sent = scan["run"](
        _listing(title="iPhone 15 Pro met schade", description="Zie foto's."),
        ai_classifier.AiVerdict(False, "classification error: overloaded_error"),
    )
    assert sent == []
    assert storage.get_seen_record("m1") is None


def test_the_gate_can_be_switched_off(scan):
    scan["config"]["ai_gates_alerts"] = False
    sent = scan["run"](
        _listing(title="iPhone 16 Pro Max 256GB Zwart", description="Nette staat, kleine schade?"),
        ai_classifier.AiVerdict(False, "shop advertisement for a working used phone"),
    )
    assert len(sent) == 1


def test_a_verified_seller_never_costs_an_ai_call(scan):
    # Rejected by the filter, so the classifier is never reached - a stub that
    # returns None would blow up if it were.
    sent = scan["run"](
        _listing(
            title="iPhone 15 Pro 256GB zwart /zeer net /100% batt /garantie",
            description="Met garantie, schade vrij.",
            seller_is_verified=True,
        ),
        None,
    )
    assert sent == []
    assert "verified/business seller" in _row("m1")[0]


def test_a_phone_case_never_costs_an_ai_call(scan):
    sent = scan["run"](
        _listing(
            listing_id="m2",
            title="Rode iPhone 15 Plus Silicone Case met MagSafe",
            description="Nieuw, lichte schade aan de doos.",
        ),
        None,
    )
    assert sent == []
    assert "accessory" in _row("m2")[0]


def test_a_keyword_match_never_needs_the_ai(scan):
    sent = scan["run"](
        _listing(title="iPhone 15 Pro Max - Achterkant beschadigd", description="Werkt verder prima."),
        None,
    )
    assert len(sent) == 1
    assert filters.evaluate_listing(
        "iPhone 15 Pro Max - Achterkant beschadigd", "", scan["config"]
    ).accepted
