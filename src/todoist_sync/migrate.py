"""One-off migration of an environment from 2.x to 3.0:

    config.env  ->  config.toml   (one channel; config.env kept as config.env.v2)
    state.json  ->  state/<slug>.json

Run by deploy/env.sh while the environment's agents are stopped:

    python -m todoist_sync.migrate <data dir>

Does nothing once config.toml exists, so it's safe to run on every build.
Imports only the side-effect-free channels module: importing config would
read the Keychain and the very config.toml this is about to write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import dotenv_values

from .channels import parse_config

# config.env setting -> config.toml setting, for the ones that carry over.
_SETTINGS = {
    "SYNC_LOG_LEVEL": "log_level",
    "SYNC_CONFLICT_WINNER": "conflict_winner",
    "SYNC_ARCHIVE_AFTER_DAYS": "archive_after_days",
    "SYNC_PRUNE_MISSING_AFTER_CHECKS": "prune_after_checks",
}
_NAMES = {"SYNC_LIST_NAME", "SYNC_REMINDERS_LIST", "SYNC_TODOIST_PROJECT"}


def _toml_value(key: str, raw: str) -> str:
    if key in ("archive_after_days", "prune_after_checks"):
        return str(int(raw))
    # A JSON string is a valid TOML basic string.
    return json.dumps(raw.strip().upper() if key == "log_level" else raw.strip().lower())


def migrate(data_dir: Path) -> bool:
    """True if it migrated, False if there was nothing to do."""
    old_config, new_config = data_dir / "config.env", data_dir / "config.toml"
    if new_config.exists() or not old_config.exists():
        return False
    values = dotenv_values(old_config)
    if "TODOIST_API_KEY" in values:
        sys.exit(f"{old_config} holds TODOIST_API_KEY: move it to the Keychain first, then rebuild")
    unknown = set(values) - set(_SETTINGS) - _NAMES
    if unknown:
        sys.exit(f"{old_config}: don't know where {', '.join(sorted(unknown))} goes in config.toml — migrate by hand")

    list_name = values.get("SYNC_LIST_NAME") or "Siri Sync"
    lines = [
        "# Todoist Sync. Migrated from config.env; settings and their defaults:",
        "# config.example.toml in the repo.",
    ]
    lines += [f"{new} = {_toml_value(new, values[old])}" for old, new in _SETTINGS.items() if values.get(old)]
    lines += [
        "",
        "[[channel]]",
        f"reminders = {json.dumps(values.get('SYNC_REMINDERS_LIST') or list_name)}",
        f"todoist = {json.dumps(values.get('SYNC_TODOIST_PROJECT') or list_name)}",
    ]
    text = "\n".join(lines) + "\n"
    _, (channel,) = parse_config(text)  # refuse to write a config that won't load

    old_state, new_state = data_dir / "state.json", data_dir / "state" / f"{channel.slug}.json"
    if new_state.exists():
        sys.exit(f"{new_state} already exists — not overwriting it with {old_state}")
    new_state.parent.mkdir(mode=0o700, exist_ok=True)
    if old_state.exists():
        old_state.rename(new_state)
        print(f"Moved {old_state.name} to state/{new_state.name}")
    new_config.write_text(text)
    new_config.chmod(0o600)
    old_config.rename(data_dir / "config.env.v2")
    print(f"Migrated config.env to config.toml (channel {channel.label}); the old file is config.env.v2")
    return True


if __name__ == "__main__":
    migrate(Path(sys.argv[1]))
