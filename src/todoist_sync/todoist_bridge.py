from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

import httpx
from todoist_api_python.api import TodoistAPI
from todoist_api_python.models import Task

log = logging.getLogger(__name__)

# get_task() below fetches this itself rather than through self.api.get_task():
# a deleted Todoist task doesn't 404 — GET /tasks/{id} still returns 200 with
# is_deleted=true in the raw JSON — but the SDK's Task model has no field for
# it at all, silently dropping the one piece of information that matters here.
_TASKS_URL = "https://api.todoist.com/api/v1/tasks"


class TodoistProjectNotFoundError(RuntimeError):
    """Raised when a project that must already exist can't be found."""


class TodoistBridge:
    """Talks to the Todoist API for a single dedicated project."""

    def __init__(self, api_token: str):
        self.api = TodoistAPI(api_token)
        self._token = api_token
        self._client = httpx.Client(timeout=30.0)
        self._section_ids: dict[str, dict[str, str]] = {}

    def close(self) -> None:
        self._client.close()

    def get_or_create_project(self, name: str, create: bool = True) -> str:
        for page in self.api.get_projects():
            for project in page:
                if project.name == name:
                    return project.id
        if not create:
            raise TodoistProjectNotFoundError(f"Todoist project {name!r} not found")
        log.debug("Creating Todoist project %r (none found)", name)
        return self.api.add_project(name=name).id

    def get_active_tasks(self, project_id: str) -> list:
        tasks = []
        for page in self.api.get_tasks(project_id=project_id):
            tasks.extend(page)
        return tasks

    def get_task(self, task_id: str, project_id: str):
        # Scoped to project_id so a tampered or stale state file entry
        # can't read/mutate a task outside the intended project — mirrors
        # the same confinement RemindersBridge applies via list_name.
        # task_id is percent-encoded with nothing left safe, so it can only
        # ever be a single path segment. Interpolated raw, a tampered or
        # stale state file entry like "../../../sync" resolves to a
        # different endpoint on the same host — with our bearer token
        # attached. (httpx doesn't follow redirects by default, so the
        # token can't reach another host either way.)
        try:
            response = self._client.get(
                f"{_TASKS_URL}/{quote(task_id, safe='')}",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.debug("Todoist task %s not found (404) — treating as deleted", task_id)
                return None
            raise
        data = response.json()
        if data.get("is_deleted"):
            log.debug("Todoist task %s is_deleted=true — treating as deleted", task_id)
            return None
        task = Task.from_dict(data)
        if task.project_id != project_id:
            log.warning(
                "Todoist task %s belongs to project %s, not %s — treating as deleted",
                task_id,
                task.project_id,
                project_id,
            )
            return None
        return task

    def _sections(self, project_id: str) -> dict[str, str]:
        """The project's sections, name -> id; fetched once per run."""
        if project_id not in self._section_ids:
            self._section_ids[project_id] = {
                section.name: section.id for page in self.api.get_sections(project_id=project_id) for section in page
            }
        return self._section_ids[project_id]

    def _section_id(self, project_id: str, name: str) -> str:
        sections = self._sections(project_id)
        if name not in sections:
            log.debug("Creating Todoist section %r in project %s", name, project_id)
            sections[name] = self.api.add_section(name=name, project_id=project_id).id
        return sections[name]

    def section_name(self, project_id: str, section_id: str | None) -> str | None:
        if section_id is None:
            return None
        for name, known_id in self._sections(project_id).items():
            if known_id == section_id:
                return name
        raise RuntimeError(f"Todoist section {section_id} isn't one of project {project_id}'s sections")

    def move_to_section(self, task_id: str, project_id: str, name: str | None) -> None:
        log.debug("Moving Todoist task %s to section %r", task_id, name)
        if name is None:
            self.api.move_task(task_id, project_id=project_id)
        else:
            self.api.move_task(task_id, section_id=self._section_id(project_id, name))

    def create_task(
        self,
        project_id: str,
        content: str,
        description: str,
        due_dt: datetime | None = None,
        all_day: bool = False,
        section: str | None = None,
    ) -> str:
        kwargs = {}
        if due_dt is not None:
            kwargs = {"due_date": due_dt.date()} if all_day else {"due_datetime": due_dt}
        if section is not None:
            kwargs["section_id"] = self._section_id(project_id, section)
        log.debug("Creating Todoist task %r in project %s", content, project_id)
        return self.api.add_task(
            content=content, project_id=project_id, description=description, **kwargs
        ).id

    def set_task_due(self, task_id: str, due_dt: datetime, all_day: bool) -> None:
        log.debug("Setting Todoist task %s due date to %s", task_id, due_dt)
        if all_day:
            self.api.update_task(task_id, due_date=due_dt.date())
        else:
            self.api.update_task(task_id, due_datetime=due_dt)

    def set_task_content(self, task_id: str, content: str) -> None:
        log.debug("Setting Todoist task %s title to %r", task_id, content)
        self.api.update_task(task_id, content=content)

    def set_task_description(self, task_id: str, description: str) -> None:
        log.debug("Setting Todoist task %s description", task_id)
        self.api.update_task(task_id, description=description)

    def complete_task(self, task_id: str) -> None:
        log.debug("Completing Todoist task %s", task_id)
        self.api.complete_task(task_id)

    def uncomplete_task(self, task_id: str) -> None:
        log.debug("Uncompleting Todoist task %s", task_id)
        self.api.uncomplete_task(task_id)

    def delete_task(self, task_id: str) -> None:
        log.debug("Deleting Todoist task %s", task_id)
        self.api.delete_task(task_id)
