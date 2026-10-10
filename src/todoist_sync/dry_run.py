"""Stand-ins for the two bridges that read for real but only log writes.

Used by --dry-run to preview a sync against live data. Reads are passed
through by an explicit allowlist rather than wholesale, so a write method
added to a bridge later fails loudly here instead of silently running for
real during a dry run.

Titles are logged at INFO (unlike a normal run, which keeps them at DEBUG):
a dry run is started by hand precisely to see which items would change.
Notes/descriptions are never logged.
"""
from __future__ import annotations

import itertools
import logging

log = logging.getLogger(__name__)

_fake_ids = itertools.count(1)


class _ReadThrough:
    _reads: frozenset[str] = frozenset()

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in self._reads:
            return getattr(self._inner, name)
        raise AttributeError(f"{name!r} has no dry-run stand-in; add one before calling it in a dry run")


class DryRunReminders(_ReadThrough):
    _reads = frozenset({"get_reminders", "get_reminder"})

    def create_reminder(self, name, body, due_dt=None, all_day=False) -> str:
        log.info("DRY RUN: would create reminder %r (due %s)", name, due_dt)
        return f"dry-run-reminder-{next(_fake_ids)}"

    def set_name(self, reminder_id, name) -> bool:
        log.info("DRY RUN: would rename reminder %s to %r", reminder_id, name)
        return True

    def set_body(self, reminder_id, body) -> bool:
        log.info("DRY RUN: would update notes of reminder %s", reminder_id)
        return True

    def complete_reminder(self, reminder_id) -> bool:
        log.info("DRY RUN: would complete reminder %s", reminder_id)
        return True

    def uncomplete_reminder(self, reminder_id) -> bool:
        log.info("DRY RUN: would uncomplete reminder %s", reminder_id)
        return True

    def set_due_date(self, reminder_id, due_dt, all_day) -> bool:
        log.info("DRY RUN: would set reminder %s due to %s (all_day=%s)", reminder_id, due_dt, all_day)
        return True


class DryRunTodoist(_ReadThrough):
    _reads = frozenset({"get_active_tasks", "get_task", "close"})

    def create_task(self, project_id, content, description, due_dt=None, all_day=False) -> str:
        log.info("DRY RUN: would create Todoist task %r (due %s)", content, due_dt)
        return f"dry-run-task-{next(_fake_ids)}"

    def set_task_content(self, task_id, content) -> None:
        log.info("DRY RUN: would rename Todoist task %s to %r", task_id, content)

    def set_task_description(self, task_id, description) -> None:
        log.info("DRY RUN: would update description of Todoist task %s", task_id)

    def complete_task(self, task_id) -> None:
        log.info("DRY RUN: would complete Todoist task %s", task_id)

    def uncomplete_task(self, task_id) -> None:
        log.info("DRY RUN: would uncomplete Todoist task %s", task_id)

    def set_task_due(self, task_id, due_dt, all_day) -> None:
        log.info("DRY RUN: would set Todoist task %s due to %s (all_day=%s)", task_id, due_dt, all_day)
