"""
Tests for the AI classifier outage watchdog (added 2026-10-03).

Background: the Haiku API failed on every call from 2026-09-20 to 09-24 and
nobody noticed for five days. seen_listings holds ZERO "AI review" rows for
that window against 68-99 a day either side, and 229 on 09-25 when the
backlog finally flushed. Alerts fell from ~30/day to 5-11/day - the
keyword-match half of the pipeline kept working, so from a phone it just
looked like a quiet week.

Neither existing watchdog could catch it. The 07-28 scan-health check
inspects what a run FETCHED (197-323 new listings a day throughout - fine),
and the 08-09 cadence watchdog inspects the gap between runs (every run
fired on time). The failures lived entirely in the transient-error path,
which defers the listing and logs at WARNING - correct behaviour, silently
repeated ~200 times a day.
"""

import pytest

import main
import storage
import telegram_notifier

HOUR = 3600


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB + captured Telegram sends + controllable clock."""
    path = tmp_path / "test.db"
    storage.init_db(path)
    monkeypatch.setattr(storage, "DB_PATH", path)

    sent = []
    monkeypatch.setattr(
        telegram_notifier, "send_message", lambda msg, **kw: sent.append(msg) or True
    )

    clock = {"now": 1_000_000}
    monkeypatch.setattr(main.time, "time", lambda: clock["now"])
    return {"sent": sent, "clock": clock, "config": {}}


def _run(env, ai_calls, ai_failures, advance_minutes=7):
    """One scan's worth of the check, then move the clock to the next run."""
    main.check_ai_health(ai_calls, ai_failures, env["config"])
    env["clock"]["now"] += advance_minutes * 60


def test_healthy_runs_never_alert(env):
    for _ in range(10):
        _run(env, ai_calls=12, ai_failures=0)
    assert env["sent"] == []
    assert storage.get_health_value("ai_failure_runs") == 0


def test_one_failed_run_does_not_alert(env):
    """A single 529-overloaded run is self-healing; the deferred listings are
    picked up seven minutes later."""
    _run(env, ai_calls=8, ai_failures=8)
    assert env["sent"] == []
    assert storage.get_health_value("ai_failure_runs") == 1


def test_the_september_outage_alerts(env):
    """The incident this exists for: every call failing, run after run."""
    for _ in range(3):
        _run(env, ai_calls=9, ai_failures=9)

    assert len(env["sent"]) == 1
    body = env["sent"][0]
    assert "ANTHROPIC_API_KEY" in body     # names the thing to go fix
    assert "keyword" in body              # says alerts are degraded, not dead


def test_five_days_of_outage_is_rate_limited(env):
    """At ~200 runs/day an uncooled warning fires ~1000 times over the real
    outage and gets muted in the first hour - which is how the NEXT one goes
    unnoticed. 12h cooldown over 5 days = 10 messages, not 1000."""
    for _ in range(5 * 24 * 60 // 7):     # five days at the ~7 min cadence
        _run(env, ai_calls=9, ai_failures=9)
    assert 9 <= len(env["sent"]) <= 11


def test_runs_with_no_ai_calls_do_not_reset_the_streak(env):
    """Most runs find no new listings, so they attempt no AI calls. Counting
    those as healthy would wipe the streak before it ever hit the threshold -
    the outage would then never alert at all."""
    _run(env, ai_calls=9, ai_failures=9)
    _run(env, ai_calls=0, ai_failures=0)
    assert storage.get_health_value("ai_failure_runs") == 1
    _run(env, ai_calls=9, ai_failures=9)
    _run(env, ai_calls=0, ai_failures=0)
    _run(env, ai_calls=9, ai_failures=9)
    assert len(env["sent"]) == 1


def test_a_partial_failure_is_not_an_outage(env):
    """Some calls getting through means the API is reachable; a single
    listing's text failing repeatedly is a different problem (and the
    unparseable-response path already buries those as normal rejects)."""
    for _ in range(10):
        _run(env, ai_calls=9, ai_failures=8)
    assert env["sent"] == []
    assert storage.get_health_value("ai_failure_runs") == 0


def test_recovery_resets_the_streak(env):
    for _ in range(2):
        _run(env, ai_calls=9, ai_failures=9)
    assert storage.get_health_value("ai_failure_runs") == 2

    _run(env, ai_calls=9, ai_failures=0)          # key replaced, API back
    assert storage.get_health_value("ai_failure_runs") == 0
    assert env["sent"] == []                      # never reached 3 runs


def test_threshold_and_cooldown_are_configurable(env):
    env["config"] = {
        "alert_ai_failure_consecutive_runs": 1,
        "alert_ai_cooldown_hours": 0,
    }
    _run(env, ai_calls=3, ai_failures=3)
    _run(env, ai_calls=3, ai_failures=3)
    assert len(env["sent"]) == 2                  # fires immediately, no cooldown


# --- End to end, through the real scan cycle --------------------------------
#
# The checks above drive check_ai_health() directly. These run main.py's
# actual loop with the network stubbed out, so they pin the wiring too: that
# a failing classifier increments the counter at all, and that the listing is
# still left unseen for the next run to retry. Getting the second part wrong
# is what would have turned the September outage from five lost days into
# five days of permanently buried listings.


@pytest.fixture
def scan(tmp_path, monkeypatch):
    from pathlib import Path

    import yaml

    import ai_classifier
    import distance
    import market
    import scraper

    from test_no_defect_gate import _isolate_db

    _isolate_db(tmp_path / "test.db", monkeypatch)

    config = yaml.safe_load(
        (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")
    )
    config["search_queries"] = ["iphone schade"]
    config["market_queries"] = []
    config["request_delay_seconds"] = 0

    state = {"sent": []}
    listing = scraper.Listing(
        listing_id="m-outage",
        title="iPhone 16 Pro 256GB",
        description_snippet="Toestel heeft schade, zie foto's.",
        price_text="€ 400,00",
        location_text="Veenendaal",
        url="https://www.marktplaats.nl/v/m-outage",
        price_cents=40000,
    )

    monkeypatch.setattr(scraper, "fetch_listings", lambda *a, **kw: [listing])
    monkeypatch.setattr(
        scraper, "fetch_listing_details", lambda *a, **kw: scraper.ListingDetails()
    )
    # What a 529/auth failure actually returns - see ai_classifier's final
    # except block.
    monkeypatch.setattr(
        ai_classifier,
        "classify_ambiguous_listing",
        lambda *a, **kw: ai_classifier.AiVerdict(
            relevant=False, reason="classification error: Connection error."
        ),
    )
    monkeypatch.setattr(
        telegram_notifier,
        "send_listing",
        lambda image, message, **kw: state["sent"].append(message) or True,
    )
    monkeypatch.setattr(
        telegram_notifier, "send_message", lambda msg, **kw: state["sent"].append(msg) or True
    )
    monkeypatch.setattr(
        distance,
        "get_driving_distance_from_coords",
        lambda *a, **kw: distance.DistanceResult(10, 12, "driving", "OK"),
    )
    for name in ("ingest_listings", "poll_bids", "check_closures"):
        monkeypatch.setattr(market, name, lambda *a, **kw: None)

    state["config"] = config
    state["listing_id"] = listing.listing_id
    return state


def test_the_real_scan_cycle_counts_a_failing_classifier(scan):
    main.run_scan_cycle(scan["config"])
    assert storage.get_health_value("ai_failure_runs") == 1


def test_a_deferred_listing_is_not_marked_seen(scan):
    """The whole reason 09-25 recovered: a failed call must leave the listing
    unseen, so the first run that works again judges it properly."""
    main.run_scan_cycle(scan["config"])
    assert storage.get_seen_record(scan["listing_id"]) is None


def test_the_outage_alert_reaches_telegram_through_the_real_cycle(scan):
    for _ in range(3):
        main.run_scan_cycle(scan["config"])
    assert any("AI classifier down" in msg for msg in scan["sent"])
