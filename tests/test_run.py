from __future__ import annotations

import httpx
import pytest

from todoist_sync import sync_tasks


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.todoist.com/api/v1/tasks")
    return httpx.HTTPStatusError("error", request=request, response=httpx.Response(status, request=request))


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """run() with main() scripted: each call pops the next outcome
    (an exception to raise, or None for a clean sync)."""

    class Runner:
        breaker = tmp_path / "circuit-breaker-open"
        outcomes: list = []
        main_calls: list = []
        sleeps: list = []
        alarms: list = []

    def fake_main(dry_run=False):
        Runner.main_calls.append(dry_run)
        outcome = Runner.outcomes.pop(0) if Runner.outcomes else None
        if outcome is not None:
            raise outcome

    class FakeLock:
        def close(self):
            pass

    monkeypatch.setattr(sync_tasks, "main", fake_main)
    monkeypatch.setattr(sync_tasks, "BREAKER_PATH", Runner.breaker)
    monkeypatch.setattr(sync_tasks, "_acquire_lock", FakeLock)
    monkeypatch.setattr(sync_tasks, "_sleep", Runner.sleeps.append)
    monkeypatch.setattr(sync_tasks, "_raise_alarm", Runner.alarms.append)
    return Runner


def test_clean_run_succeeds_and_leaves_the_breaker_closed(runner):
    assert sync_tasks.run([]) == 0
    assert runner.main_calls == [False]
    assert not runner.breaker.exists()


def test_open_breaker_skips_the_sync(runner):
    runner.breaker.write_text("2026-10-10T21:00:00 ValueError: boom\n")

    assert sync_tasks.run([]) == 1
    assert runner.main_calls == []


def test_dry_run_ignores_an_open_breaker(runner):
    runner.breaker.write_text("2026-10-10T21:00:00 ValueError: boom\n")

    assert sync_tasks.run(["--dry-run"]) == 0
    assert runner.main_calls == [True]


@pytest.mark.parametrize(
    "transient",
    [httpx.ConnectError("offline"), httpx.ReadTimeout("slow"), _http_error(429), _http_error(503)],
    ids=["offline", "timeout", "rate-limited", "server-error"],
)
def test_transient_error_is_retried_after_a_backoff(runner, transient):
    runner.outcomes = [transient, None]

    assert sync_tasks.run([]) == 0
    assert len(runner.main_calls) == 2
    assert runner.sleeps == [sync_tasks._RETRY_DELAYS[0]]
    assert not runner.breaker.exists()


def test_persistent_transient_errors_give_up_until_next_run_without_tripping(runner):
    runner.outcomes = [httpx.ConnectError("offline")] * (len(sync_tasks._RETRY_DELAYS) + 1)

    assert sync_tasks.run([]) == 1
    assert runner.sleeps == list(sync_tasks._RETRY_DELAYS)
    assert not runner.breaker.exists()
    assert runner.alarms == []


@pytest.mark.parametrize(
    "unexpected",
    [ValueError("boom"), _http_error(401), sync_tasks.MassDeleteError("5 open items vanished")],
    ids=["bug", "bad-token", "mass-delete"],
)
def test_unexpected_error_trips_the_breaker_raises_the_alarm_and_crashes(runner, unexpected):
    runner.outcomes = [unexpected]

    with pytest.raises(type(unexpected)):
        sync_tasks.run([])

    assert runner.breaker.exists()
    assert len(runner.alarms) == 1
    assert runner.sleeps == []


def test_unexpected_error_in_a_dry_run_crashes_without_tripping(runner):
    runner.outcomes = [ValueError("boom")]

    with pytest.raises(ValueError):
        sync_tasks.run(["--dry-run"])

    assert not runner.breaker.exists()
    assert runner.alarms == []
