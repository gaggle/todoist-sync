from __future__ import annotations

from types import SimpleNamespace

import pytest

from todoist_sync.config import resolve_container_names
from todoist_sync.todoist_bridge import TodoistBridge, TodoistProjectNotFoundError


def test_list_name_names_both_sides_and_allows_creating_the_project():
    assert resolve_container_names({"SYNC_LIST_NAME": "Errands"}) == ("Errands", "Errands", True)


def test_defaults_to_siri_sync_when_nothing_is_set():
    assert resolve_container_names({}) == ("Siri Sync", "Siri Sync", True)


def test_separate_names_pair_differently_named_containers():
    environ = {"SYNC_REMINDERS_LIST": "Chores", "SYNC_TODOIST_PROJECT": "Family Project"}

    assert resolve_container_names(environ) == ("Chores", "Family Project", False)


def test_overriding_only_the_reminders_list_keeps_list_name_for_the_project():
    environ = {"SYNC_LIST_NAME": "Errands", "SYNC_REMINDERS_LIST": "Chores"}

    assert resolve_container_names(environ) == ("Chores", "Errands", True)


class _FakeProjectsApi:
    def __init__(self, names):
        self.projects = [SimpleNamespace(id=f"p-{n}", name=n) for n in names]
        self.added = []

    def get_projects(self):
        yield self.projects

    def add_project(self, name):
        self.added.append(name)
        return SimpleNamespace(id=f"p-{name}")


def _bridge_with(api) -> TodoistBridge:
    bridge = TodoistBridge("fake-token")
    bridge.api = api
    return bridge


def test_finds_an_existing_project_without_creating_one():
    api = _FakeProjectsApi(["Inbox", "Family Project"])

    assert _bridge_with(api).get_or_create_project("Family Project", create=False) == "p-Family Project"
    assert api.added == []


def test_missing_project_raises_instead_of_creating_when_create_is_false():
    api = _FakeProjectsApi(["Inbox"])

    with pytest.raises(TodoistProjectNotFoundError):
        _bridge_with(api).get_or_create_project("Family Projcet", create=False)
    assert api.added == []


def test_missing_project_is_created_by_default():
    api = _FakeProjectsApi(["Inbox"])

    assert _bridge_with(api).get_or_create_project("Siri Sync") == "p-Siri Sync"
    assert api.added == ["Siri Sync"]
