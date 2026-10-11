from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

TOMORROW = date.today() + timedelta(days=1)


def test_reminder_with_a_time_becomes_a_task_at_the_same_time(bench, sync):
    bench.reminders.create_reminder("Buy milk", "From the corner shop", datetime.combine(TOMORROW, datetime.min.time()).replace(hour=10))

    sync()

    task = bench.tasks()["Buy milk"]
    assert task.description == "From the corner shop"
    assert task.due.date == datetime.combine(TOMORROW, datetime.min.time()).replace(hour=10)


def test_task_with_a_time_becomes_a_reminder_at_the_same_time(bench, sync):
    bench.add_task("Call the vet", due_string="tomorrow at 15:00")

    sync()

    reminder = bench.reminders_by_name()["Call the vet"]
    assert reminder["due"] == datetime.combine(TOMORROW, datetime.min.time()).replace(hour=15)
    assert reminder["all_day"] is False


def test_a_second_sync_changes_nothing(bench, sync, caplog):
    bench.reminders.create_reminder("From Reminders", "", None)
    bench.add_task("From Todoist", due_string="tomorrow")
    sync()

    with caplog.at_level("INFO"):
        sync()

    assert "Sync complete (Sync Test ↔ Sync Test): no changes" in caplog.text


def test_completing_a_reminder_completes_its_task(bench, sync):
    reminder_id = bench.reminders.create_reminder("Water plants", "", None)
    sync()
    task_id = bench.tasks()["Water plants"].id

    bench.reminders.complete_reminder(reminder_id)
    sync()

    assert bench.task(task_id).completed_at is not None


def test_completing_a_task_completes_its_reminder(bench, sync):
    bench.add_task("Empty the dishwasher")
    sync()

    bench.todoist.complete_task(bench.tasks()["Empty the dishwasher"].id)
    sync()

    assert bench.reminders_by_name()["Empty the dishwasher"]["completed"] is True


def test_deleting_a_reminder_deletes_its_task_once_confirmed(bench, sync, monkeypatch):
    from todoist_sync import config, sync_tasks

    monkeypatch.setattr(config.CHANNELS[0], "prune_after_checks", 2)
    reminder_id = bench.reminders.create_reminder("Short-lived", "", None)
    sync()
    task_id = bench.tasks()["Short-lived"].id

    bench.reminders.delete_reminder(reminder_id)
    sync()
    assert bench.task(task_id) is not None
    sync()

    assert bench.task(task_id) is None


def test_many_open_items_vanishing_at_once_trips_the_breaker_and_deletes_nothing(bench, sync, monkeypatch):
    from todoist_sync import config, sync_tasks

    monkeypatch.setattr(config.CHANNELS[0], "prune_after_checks", 1)
    titles = [f"Mass {i}" for i in range(sync_tasks._MAX_OPEN_DELETES_PER_RUN + 1)]
    ids = [bench.reminders.create_reminder(title, "", None) for title in titles]
    sync()
    for reminder_id in ids:
        bench.reminders.delete_reminder(reminder_id)

    with pytest.raises(sync_tasks.MassDeleteError):
        sync_tasks.run([])

    assert config.BREAKER_PATH.exists()
    assert sorted(bench.tasks()) == titles
