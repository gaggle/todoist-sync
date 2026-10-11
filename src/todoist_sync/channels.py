"""What an environment syncs: its config.toml, parsed into channels.

A channel is one Reminders list kept in sync with one Todoist project. Its
settings default to the file's top-level values and can be overridden per
channel:

    log_level = "INFO"

    [[channel]]
    reminders = "Chores"
    todoist = "Family Project"

    [[channel]]
    reminders = "Shopping"
    todoist = "Shopping"
    sections = false            # a Groceries list: don't sync its aisles
    archive_after_days = 7

Kept free of side effects (no paths, no Keychain, no logging setup), so the
config.env migration and the tests can use it on its own.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    pass


@dataclass
class Channel:
    reminders: str
    todoist: str
    # Board columns <-> list sections. Off for a Groceries list: Reminders
    # sorts it into aisles of its own, and looking up every reminder's
    # section costs a ReminderKit call each, completed ones included.
    sections: bool = True
    conflict_winner: str = "reminders"
    # Pairs completed on both sides this long are archived: no longer
    # polled, but still recognised if reopened.
    archive_after_days: int = 180
    # An item gone from one side for this many consecutive runs is deleted
    # on the other too. A count, not a time: it demands that many
    # independent confirmations however far apart the runs are.
    prune_after_checks: int = 4

    @property
    def label(self) -> str:
        return f"{self.reminders} ↔ {self.todoist}"

    @property
    def slug(self) -> str:
        """Names the channel's state file. Derived from the Reminders list,
        so renaming that list in config.toml means renaming the state file
        too — otherwise every pair would look new, and be duplicated."""
        return re.sub(r"[^a-z0-9]+", "-", self.reminders.lower()).strip("-") or "channel"


_SETTINGS = {"sections": bool, "conflict_winner": str, "archive_after_days": int, "prune_after_checks": int}
_TOP_LEVEL_KEYS = {"log_level", "channel", *_SETTINGS}
_CHANNEL_KEYS = {"reminders", "todoist", *_SETTINGS}


def _check_setting(where: str, key: str, value) -> None:
    expected = _SETTINGS[key]
    # bool is an int in Python; a count given as `true` is still a typo.
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise ConfigError(f"{where}: {key} must be a {expected.__name__}, got {value!r}")
    if key == "conflict_winner" and value not in ("reminders", "todoist"):
        raise ConfigError(f"{where}: conflict_winner must be 'reminders' or 'todoist', got {value!r}")
    if expected is int and value <= 0:
        raise ConfigError(f"{where}: {key} must be positive, got {value!r}")


def parse_config(text: str) -> tuple[str, list[Channel]]:
    """Returns (log level name, channels). Raises ConfigError (or
    tomllib.TOMLDecodeError) on anything unexpected, unknown keys included:
    a misspelt setting should fail loudly, not quietly fall back."""
    data = tomllib.loads(text)
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    log_level = data.get("log_level", "INFO")
    if not isinstance(log_level, str) or log_level.upper() not in LOG_LEVELS:
        raise ConfigError(f"log_level must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}")
    defaults = {}
    for key in _SETTINGS:
        if key in data:
            _check_setting("top level", key, data[key])
            defaults[key] = data[key]

    tables = data.get("channel")
    if not isinstance(tables, list) or not tables:
        raise ConfigError("no channels: add a [[channel]] with reminders = \"…\" and todoist = \"…\"")
    channels = []
    for n, table in enumerate(tables, 1):
        where = f"channel {n}"
        unknown = set(table) - _CHANNEL_KEYS
        if unknown:
            raise ConfigError(f"{where}: unknown setting(s): {', '.join(sorted(unknown))}")
        for side in ("reminders", "todoist"):
            if not isinstance(table.get(side), str) or not table[side].strip():
                raise ConfigError(f"{where}: {side} must name a {'Reminders list' if side == 'reminders' else 'Todoist project'}")
        for key in _SETTINGS:
            if key in table:
                _check_setting(where, key, table[key])
        channels.append(Channel(**{**defaults, **table}))

    # Two channels on one container would each read the other's items as
    # unpaired and duplicate them; two sharing a state file would prune them.
    for attr, what in (("reminders", "Reminders list"), ("todoist", "Todoist project"), ("slug", "state file")):
        seen = [getattr(c, attr) for c in channels]
        dupes = sorted({v for v in seen if seen.count(v) > 1})
        if dupes:
            raise ConfigError(f"more than one channel uses the {what} {', '.join(map(repr, dupes))}")
    return log_level.upper(), channels
