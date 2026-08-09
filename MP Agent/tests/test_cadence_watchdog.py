"""
Tests for the dispatcher cadence watchdog (added 2026-08-09).

Background: the external cron-job.org dispatcher stopped firing at
2026-08-06T17:49Z and it took 2.5 days to notice. Nothing failed loudly -
GitHub's own `schedule:` cron kept the workflow alive at ~18-24 runs/day
instead of the dispatcher's ~200, so Telegram alerts still arrived, just 8x
slower. The 07-28 health check could never catch it: it inspects what a run
fetched, and a run that never happened fetches nothing to inspect.
"""

import pytest

import main
import storage
import telegram_notifier

MIN = 60


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


def _advance(env, minutes):
    env["clock"]["now"] += int(minutes * MIN)


def test_first_run_on_a_fresh_db_never_alerts(env):
    main.check_run_cadence(env["config"])
    assert env["sent"] == []
    assert storage.get_health_value("last_run_epoch") == 1_000_000


def test_normal_dispatcher_cadence_is_silent(env):
    main.check_run_cadence(env["config"])
    for _ in range(10):
        _advance(env, 7)          # the dispatcher's real ~7 min interval
        main.check_run_cadence(env["config"])
    assert env["sent"] == []
    assert storage.get_health_value("slow_cadence_runs") == 0


def test_one_skipped_fire_does_not_alert(env):
    main.check_run_cadence(env["config"])
    _advance(env, 45)
    main.check_run_cadence(env["config"])
    assert env["sent"] == []      # hysteresis: needs 3 consecutive slow gaps


def test_sustained_fallback_cadence_alerts_once(env):
    main.check_run_cadence(env["config"])
    for _ in range(3):            # three consecutive ~hourly GitHub-cron runs
        _advance(env, 60)
        main.check_run_cadence(env["config"])

    assert len(env["sent"]) == 1
    body = env["sent"][0]
    assert "cron-job.org" in body        # names the actual thing to go fix
    assert "PAT" in body


def test_cooldown_prevents_a_warning_on_every_fallback_run(env):
    """The failure this guards: at ~24 runs/day the condition is true on
    every single one, so an uncooled warning would fire 24x a day and be
    muted within an hour - which is how the next outage gets missed."""
    main.check_run_cadence(env["config"])
    for _ in range(24):
        _advance(env, 60)
        main.check_run_cadence(env["config"])

    assert len(env["sent"]) == 2  # 24h of fallback at the 12h default cooldown


def test_recovery_resets_the_streak(env):
    main.check_run_cadence(env["config"])
    for _ in range(2):
        _advance(env, 60)
        main.check_run_cadence(env["config"])
    assert storage.get_health_value("slow_cadence_runs") == 2

    _advance(env, 7)              # dispatcher comes back
    main.check_run_cadence(env["config"])
    assert storage.get_health_value("slow_cadence_runs") == 0
    assert env["sent"] == []      # never reached the 3-run threshold
