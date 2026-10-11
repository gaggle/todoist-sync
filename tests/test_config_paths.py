from __future__ import annotations

from pathlib import Path

from todoist_sync.config import resolve_paths

REPO = Path("/repo")


def test_an_environment_keeps_config_state_log_and_bridge_where_its_app_says():
    environ = {
        "TODOIST_SYNC_ENV": "staging",
        "TODOIST_SYNC_HOME": "/support/staging",
        "TODOIST_SYNC_LOG_FILE": "/logs/staging.log",
        "TODOIST_SYNC_BRIDGE": "/support/staging/app/reminders-bridge",
    }

    paths = resolve_paths(environ, REPO)

    assert paths.environment == "staging"
    assert paths.data_dir == Path("/support/staging")
    assert paths.config_file == Path("/support/staging/config.toml")
    assert paths.state_dir == Path("/support/staging/state")
    assert paths.log_file == Path("/logs/staging.log")
    assert paths.bridge == Path("/support/staging/app/reminders-bridge")


def test_without_an_environment_nothing_points_at_real_config():
    paths = resolve_paths({}, REPO)

    assert paths.environment is None
    assert paths.data_dir == REPO / "var"
    assert paths.config_file == REPO / "var" / "config.toml"
    assert paths.log_file == REPO / "var" / "sync.log"
    assert paths.bridge == REPO / "swift/reminders-bridge/.build/release/reminders-bridge"


def test_log_file_and_bridge_default_inside_the_environment():
    paths = resolve_paths({"TODOIST_SYNC_ENV": "prod", "TODOIST_SYNC_HOME": "/support/prod"}, REPO)

    assert paths.log_file == Path("/support/prod/sync.log")
    assert paths.bridge == REPO / "swift/reminders-bridge/.build/release/reminders-bridge"
