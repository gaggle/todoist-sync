from __future__ import annotations

import pytest

from todoist_sync.channels import Channel, parse_config
from todoist_sync.migrate import migrate

PROD_CONFIG_ENV = """# Todoist Sync — prod. Other settings and their defaults: config.env.example in the repo.
SYNC_REMINDERS_LIST="Chores"
SYNC_TODOIST_PROJECT="Family Project"
SYNC_LOG_LEVEL="INFO"
"""


def test_config_env_becomes_one_channel_and_its_state_moves_with_it(tmp_path):
    (tmp_path / "config.env").write_text(PROD_CONFIG_ENV)
    (tmp_path / "state.json").write_text('{"pairs": [], "archive": []}')

    assert migrate(tmp_path) is True

    log_level, channels = parse_config((tmp_path / "config.toml").read_text())
    assert log_level == "INFO"
    assert channels == [Channel(reminders="Chores", todoist="Family Project")]
    assert (tmp_path / "state" / "chores.json").read_text() == '{"pairs": [], "archive": []}'
    assert not (tmp_path / "state.json").exists()
    assert (tmp_path / "config.env.v2").read_text() == PROD_CONFIG_ENV
    assert not (tmp_path / "config.env").exists()


def test_every_setting_carries_over(tmp_path):
    (tmp_path / "config.env").write_text(
        'SYNC_LIST_NAME="Errands"\nSYNC_CONFLICT_WINNER="Todoist"\n'
        'SYNC_ARCHIVE_AFTER_DAYS="30"\nSYNC_PRUNE_MISSING_AFTER_CHECKS="6"\nSYNC_LOG_LEVEL="debug"\n'
    )

    migrate(tmp_path)

    log_level, (channel,) = parse_config((tmp_path / "config.toml").read_text())
    assert log_level == "DEBUG"
    assert channel == Channel("Errands", "Errands", conflict_winner="todoist", archive_after_days=30, prune_after_checks=6)


def test_does_nothing_once_migrated(tmp_path):
    (tmp_path / "config.toml").write_text("whatever is there stays")
    (tmp_path / "config.env").write_text(PROD_CONFIG_ENV)

    assert migrate(tmp_path) is False
    assert (tmp_path / "config.toml").read_text() == "whatever is there stays"


def test_a_fresh_environment_has_nothing_to_migrate(tmp_path):
    assert migrate(tmp_path) is False
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize(
    "config_env",
    ['SYNC_LIST_NAME="Errands"\nTODOIST_API_KEY="secret"\n', 'SYNC_LIST_NAME="Errands"\nSYNC_MYSTERY="1"\n'],
    ids=["api-key", "unknown-setting"],
)
def test_refuses_what_it_cant_carry_over_and_touches_nothing(tmp_path, config_env):
    (tmp_path / "config.env").write_text(config_env)

    with pytest.raises(SystemExit):
        migrate(tmp_path)

    assert (tmp_path / "config.env").read_text() == config_env
    assert not (tmp_path / "config.toml").exists()
