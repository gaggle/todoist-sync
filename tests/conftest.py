from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import pytest

from todoist_sync import state as state_module
from todoist_sync import sync_tasks
from todoist_sync.channels import Channel


@pytest.fixture(autouse=True, scope="session")
def _no_real_log_files():
    # sync_tasks._configure_logging() attaches a real RotatingFileHandler to
    # the root logger at import time (not inside main()), pointed at the
    # actual var/sync.log. Without this, every test that calls main() would
    # append real log lines to that file.
    logging.getLogger().handlers.clear()


@dataclass
class FakeDue:
    date: datetime | date
    is_recurring: bool = False
    string: str = ""


@dataclass
class FakeTask:
    id: str
    content: str = "Task"
    description: str = ""
    due: FakeDue | None = None
    completed_at: datetime | None = None
    # The fake keeps a section's name as its id.
    section_id: str | None = None


@dataclass
class FakeStore:
    reminders: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    _next_reminder_id: int = 1
    _next_task_id: int = 1
    opened_list: str | None = None
    opened_project: tuple[str, bool] | None = None

    def add_reminder(
        self, name="Reminder", body="", completed=False, due=None, all_day=False, id=None, recurrence=None, section=None
    ):
        rid = id or f"r{self._next_reminder_id}"
        self._next_reminder_id += 1
        self.reminders[rid] = {
            "id": rid,
            "name": name,
            "body": body,
            "completed": completed,
            "due": due,
            "all_day": all_day,
            "recurrence": recurrence,
            "section": section,
        }
        return rid

    def add_task(self, content="Task", description="", due=None, completed=False, id=None, section=None):
        tid = id or f"t{self._next_task_id}"
        self._next_task_id += 1
        completed_at = datetime(2026, 1, 1) if completed else None
        self.tasks[tid] = FakeTask(tid, content, description, due, completed_at, section)
        return tid


def _reject_dry_run_placeholder(item_id):
    # The real Todoist API answers 400 for an id it can't parse — which is
    # what a dry run's made-up ids are — so looking one up is always a bug.
    if item_id.startswith("dry-run-"):
        raise AssertionError(f"looked up dry-run placeholder id {item_id!r}")


class FakeRemindersBridge:
    def __init__(self, list_name, sections=True):
        self.list_name = list_name
        self.sections = sections
        STORE.opened_list = list_name

    def get_reminders(self):
        # Like the real bridge: without sections, it doesn't look them up.
        return [{**r, "section": r["section"] if self.sections else None} for r in STORE.reminders.values()]

    def create_reminder(self, name, body, due_dt=None, all_day=False):
        return STORE.add_reminder(name=name, body=body, due=due_dt, all_day=all_day)

    def set_body(self, reminder_id, body):
        STORE.reminders[reminder_id]["body"] = body
        return True

    def set_name(self, reminder_id, name):
        STORE.reminders[reminder_id]["name"] = name
        return True

    def complete_reminder(self, reminder_id):
        STORE.reminders[reminder_id]["completed"] = True
        return True

    def uncomplete_reminder(self, reminder_id):
        STORE.reminders[reminder_id]["completed"] = False
        return True

    def set_due_date(self, reminder_id, due_dt, all_day):
        STORE.reminders[reminder_id]["due"] = due_dt
        STORE.reminders[reminder_id]["all_day"] = all_day
        return True

    def delete_reminder(self, reminder_id):
        del STORE.reminders[reminder_id]
        return True

    def set_section(self, reminder_id, name):
        STORE.reminders[reminder_id]["section"] = name
        return True


class FakeTodoistBridge:
    def __init__(self, api_token):
        self.api_token = api_token

    def get_or_create_project(self, name, create=True):
        STORE.opened_project = (name, create)
        return "project1"

    def get_active_tasks(self, project_id):
        # Mirrors the real Todoist API: only ever returns open tasks.
        return [t for t in STORE.tasks.values() if t.completed_at is None]

    def get_task(self, task_id, project_id):
        _reject_dry_run_placeholder(task_id)
        return STORE.tasks.get(task_id)

    def create_task(self, project_id, content, description, due_dt=None, all_day=False, section=None):
        due = FakeDue(due_dt.date() if (due_dt and all_day) else due_dt) if due_dt else None
        return STORE.add_task(content=content, description=description, due=due, section=section)

    def section_name(self, project_id, section_id):
        return section_id

    def move_to_section(self, task_id, project_id, name):
        STORE.tasks[task_id].section_id = name

    def set_task_due(self, task_id, due_dt, all_day):
        was_recurring = STORE.tasks[task_id].due.is_recurring if STORE.tasks[task_id].due else False
        STORE.tasks[task_id].due = FakeDue(due_dt.date() if all_day else due_dt, was_recurring)

    def set_task_content(self, task_id, content):
        STORE.tasks[task_id].content = content

    def set_task_description(self, task_id, description):
        STORE.tasks[task_id].description = description

    def complete_task(self, task_id):
        STORE.tasks[task_id].completed_at = datetime(2026, 1, 1)

    def uncomplete_task(self, task_id):
        STORE.tasks[task_id].completed_at = None

    def delete_task(self, task_id):
        del STORE.tasks[task_id]

    def close(self):
        pass


STORE = FakeStore()


@pytest.fixture
def env(tmp_path, monkeypatch):
    global STORE
    STORE = FakeStore()

    state_path = tmp_path / "state.json"
    monkeypatch.setattr(sync_tasks, "RemindersBridge", FakeRemindersBridge)
    monkeypatch.setattr(sync_tasks, "TodoistBridge", FakeTodoistBridge)
    monkeypatch.setattr(sync_tasks, "state_path", lambda channel: state_path)
    monkeypatch.setattr(sync_tasks, "TODOIST_API_KEY", "fake-token")

    class Env:
        store = STORE

        def __init__(self):
            # Settings a test changes apply from its next env.run().
            self.channel = Channel(reminders="Test List", todoist="Test List")

        def run(self, dry_run=False):
            sync_tasks.main(self.channel, dry_run=dry_run)

        def load_state(self):
            return state_module.load_state(state_path)

        def write_state(self, pairs, archive=None):
            state_module.save_state(state_path, pairs, archive or [])

    return Env()
