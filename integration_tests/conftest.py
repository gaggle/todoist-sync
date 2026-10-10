"""End-to-end scenarios against real Reminders and Todoist containers.

Run with `devenv shell -- integration-test`, which starts the staging app
so pytest inherits its Reminders permission. Each scenario starts and ends
with both "Sync Test" containers emptied and staging's sync state reset —
which is why this refuses to run anywhere but staging, against anything
but "Sync Test".
"""
from __future__ import annotations

import os

import pytest

BENCH = "Sync Test"
# Todoist Free caps an account at 5 projects; creating the bench project
# must never be what pushes an account over it.
MAX_TODOIST_PROJECTS = 5

if os.environ.get("TODOIST_SYNC_ENV") != "staging":
    collect_ignore_glob = ["test_*.py"]


class Bench:
    def __init__(self, reminders, todoist, project_id):
        self.reminders = reminders
        self.todoist = todoist
        self.project_id = project_id

    def tasks(self) -> dict:
        return {t.content: t for t in self.todoist.get_active_tasks(self.project_id)}

    def task(self, task_id):
        return self.todoist.get_task(task_id, self.project_id)

    def reminders_by_name(self) -> dict:
        return {r["name"]: r for r in self.reminders.get_reminders()}

    def add_task(self, content, **kwargs) -> str:
        return self.todoist.api.add_task(content=content, project_id=self.project_id, **kwargs).id

    def todoist_sections(self) -> dict:
        """Section id -> name, fetched fresh (the sync creates sections too)."""
        return {s.id: s.name for page in self.todoist.api.get_sections(project_id=self.project_id) for s in page}

    def task_section(self, title):
        return self.todoist_sections().get(self.tasks()[title].section_id)

    def move_task(self, title, section):
        section_id = {name: id for id, name in self.todoist_sections().items()}.get(section) if section else None
        if section and section_id is None:
            section_id = self.todoist.api.add_section(name=section, project_id=self.project_id).id
        task_id = self.tasks()[title].id
        if section_id:
            self.todoist.api.move_task(task_id, section_id=section_id)
        else:
            self.todoist.api.move_task(task_id, project_id=self.project_id)

    def empty(self) -> None:
        for task in self.todoist.get_active_tasks(self.project_id):
            self.todoist.delete_task(task.id)
        # Todoist sections go too, so each scenario exercises creating them.
        # The list's Reminders sections stay: the bridge only ever adds them.
        for section_id in self.todoist_sections():
            self.todoist.api.delete_section(section_id)
        for reminder in self.reminders.get_reminders():
            self.reminders.delete_reminder(reminder["id"])


def _upsert_project(todoist) -> str:
    projects = [p for page in todoist.api.get_projects() for p in page]
    for project in projects:
        if project.name == BENCH:
            return project.id
    if len(projects) >= MAX_TODOIST_PROJECTS:
        raise RuntimeError(
            f"Not creating Todoist project {BENCH!r}: the account already has {len(projects)} projects "
            f"(Todoist Free allows {MAX_TODOIST_PROJECTS})"
        )
    return todoist.api.add_project(name=BENCH).id


@pytest.fixture(scope="session")
def _containers():
    from todoist_sync import config
    from todoist_sync.reminders_bridge import RemindersBridge
    from todoist_sync.todoist_bridge import TodoistBridge

    if (config.ENVIRONMENT, config.REMINDERS_LIST, config.TODOIST_PROJECT) != ("staging", BENCH, BENCH):
        pytest.exit(
            f"Refusing to run: integration tests empty their containers, so they only run in staging "
            f"against {BENCH!r} (got env={config.ENVIRONMENT!r}, list={config.REMINDERS_LIST!r}, "
            f"project={config.TODOIST_PROJECT!r})",
            returncode=2,
        )
    todoist = TodoistBridge(config.TODOIST_API_KEY)
    project_id = _upsert_project(todoist)
    reminders = RemindersBridge(BENCH)  # creates the list if it's missing
    yield Bench(reminders, todoist, project_id)
    todoist.close()


@pytest.fixture
def bench(_containers, monkeypatch):
    from todoist_sync import config, sync_tasks

    def reset():
        _containers.empty()
        for path in (config.STATE_PATH, config.BREAKER_PATH):
            path.unlink(missing_ok=True)

    # A tripped breaker would otherwise post a real alert reminder.
    monkeypatch.setattr(sync_tasks, "_raise_alarm", lambda reason: None)
    reset()
    yield _containers
    reset()


@pytest.fixture
def sync():
    """One real sync of the bench, in-process."""
    from todoist_sync import sync_tasks

    return sync_tasks.main
