from __future__ import annotations

import pytest

from todoist_sync.channels import Channel, ConfigError, parse_config

CHORES = """
[[channel]]
reminders = "Chores"
todoist = "Family Project"
"""


def test_a_channel_pairs_a_list_with_a_project_using_the_defaults():
    log_level, channels = parse_config(CHORES)

    assert log_level == "INFO"
    assert channels == [Channel(reminders="Chores", todoist="Family Project")]
    assert channels[0].sections is True
    assert channels[0].prune_after_checks == 4


def test_top_level_settings_are_every_channels_defaults_and_a_channel_can_override_them():
    _, (chores, shopping) = parse_config(
        """
        log_level = "debug"
        archive_after_days = 30

        [[channel]]
        reminders = "Chores"
        todoist = "Family Project"

        [[channel]]
        reminders = "Shopping"
        todoist = "Shopping"
        sections = false
        archive_after_days = 7
        """
    )

    assert (chores.sections, chores.archive_after_days) == (True, 30)
    assert (shopping.sections, shopping.archive_after_days) == (False, 7)


def test_log_level_is_case_insensitive():
    assert parse_config('log_level = "debug"\n' + CHORES)[0] == "DEBUG"


def test_each_channel_keeps_its_state_in_a_file_named_after_its_list():
    assert Channel(reminders="Chores", todoist="Family Project").slug == "chores"
    assert Channel(reminders="Sync Test", todoist="Sync Test").slug == "sync-test"
    assert Channel(reminders="Kids' Stuff!", todoist="x").slug == "kids-stuff"


@pytest.mark.parametrize(
    "text, complaint",
    [
        ("", "no channels"),
        ('loglevel = "INFO"\n' + CHORES, "unknown setting(s): loglevel"),
        (CHORES + 'section = false\n', "channel 1: unknown setting(s): section"),
        ('[[channel]]\nreminders = "Chores"\n', "channel 1: todoist must name a Todoist project"),
        (CHORES + "sections = 0\n", "sections must be a bool"),
        (CHORES + "prune_after_checks = 0\n", "prune_after_checks must be positive"),
        (CHORES + "archive_after_days = true\n", "archive_after_days must be a int"),
        (CHORES + 'conflict_winner = "me"\n', "conflict_winner must be 'reminders' or 'todoist'"),
        ('log_level = "LOUD"\n' + CHORES, "log_level must be one of"),
        (CHORES + CHORES.replace("Family Project", "Other"), "more than one channel uses the Reminders list 'Chores'"),
        (CHORES + CHORES.replace("Chores", "Errands"), "more than one channel uses the Todoist project 'Family Project'"),
        (
            CHORES + '[[channel]]\nreminders = "chores!"\ntodoist = "Other"\n',
            "more than one channel uses the state file 'chores'",
        ),
    ],
    ids=[
        "empty",
        "misspelt-top-level",
        "misspelt-channel-setting",
        "missing-side",
        "sections-not-bool",
        "zero-checks",
        "bool-as-count",
        "unknown-winner",
        "unknown-log-level",
        "list-twice",
        "project-twice",
        "state-file-twice",
    ],
)
def test_anything_unexpected_fails_loudly(text, complaint):
    with pytest.raises(ConfigError, match=None) as raised:
        parse_config(text)

    assert complaint in str(raised.value)


def test_the_example_config_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    _, channels = parse_config(example.read_text())

    assert [c.sections for c in channels] == [True, False]
