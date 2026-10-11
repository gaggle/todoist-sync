import logging
import os
import subprocess
import tomllib
from pathlib import Path
from typing import NamedTuple

from .channels import Channel, ConfigError, parse_config

# src/todoist_sync/config.py -> project root is three levels up
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Paths(NamedTuple):
    environment: str | None
    data_dir: Path
    config_file: Path
    state_dir: Path
    log_file: Path
    bridge: Path


def resolve_paths(environ, project_root: Path) -> Paths:
    """Where this run's config, state, log and Reminders bridge live.

    Each environment app (staging, prod — built by deploy/env.sh) names
    itself in TODOIST_SYNC_ENV and points TODOIST_SYNC_HOME at its own data
    dir: config.toml, the state/ of each channel, the lock and the circuit
    breaker live there, so environments never share state. Without an environment, paths
    fall back to the repo's var/ (enough for tests to import this module),
    but run() refuses to sync: a run straight from a checkout must not be
    able to touch a list that an environment owns.
    """
    home = environ.get("TODOIST_SYNC_HOME")
    data_dir = Path(home) if home else project_root / "var"
    return Paths(
        environment=environ.get("TODOIST_SYNC_ENV") if home else None,
        data_dir=data_dir,
        config_file=data_dir / "config.toml",
        state_dir=data_dir / "state",
        log_file=Path(environ.get("TODOIST_SYNC_LOG_FILE") or data_dir / "sync.log"),
        bridge=Path(
            environ.get("TODOIST_SYNC_BRIDGE")
            or project_root / "swift/reminders-bridge/.build/release/reminders-bridge"
        ),
    )


PATHS = resolve_paths(os.environ, PROJECT_ROOT)
ENVIRONMENT = PATHS.environment
CONFIG_PATH = PATHS.config_file

_KEYCHAIN_SERVICE = "todoist-sync"
_KEYCHAIN_ACCOUNT = "default"

# Absolute path, not a bare "security" resolved through $PATH. launchd runs
# this with a safe default $PATH, but a manual run inherits the user's,
# which may put a user-writable directory ahead of /usr/bin. A file named
# "security" landing there would otherwise intercept the token read.
_SECURITY_BIN = "/usr/bin/security"


def _load_from_keychain() -> str | None:
    """Read the Todoist API token from the macOS Keychain.

    Returns None if the Keychain item doesn't exist or the `security`
    tool is unavailable (non-macOS). A CalledProcessError from `security`
    means the item wasn't found (exit code 44) — not a crash — so we
    treat it the same as "not set" and fall back to the environment.
    """
    try:
        result = subprocess.run(
            [_SECURITY_BIN, "find-generic-password",
             "-s", _KEYCHAIN_SERVICE, "-a", _KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

VAR_DIR = PATHS.data_dir
VAR_DIR.mkdir(parents=True, exist_ok=True)
VAR_DIR.chmod(0o700)
STATE_DIR = PATHS.state_dir
STATE_DIR.mkdir(exist_ok=True)


def state_path(channel: Channel) -> Path:
    return STATE_DIR / f"{channel.slug}.json"


LOG_PATH = PATHS.log_file
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
# Present = circuit breaker open: syncing is paused until a human removes
# it. Lives in the data dir rather than /tmp, which macOS clears on reboot —
# that would quietly resume a sync that was stopped for a reason.
BREAKER_PATH = VAR_DIR / "circuit-breaker-open"
LOCK_PATH = VAR_DIR / "sync.lock"

# The token and config.toml are read leniently here (no raise) and
# validated explicitly via validate(), which sync_tasks.py calls only after
# logging is configured. Raising here at import time — before any log
# handler exists — would send a bad config's traceback straight to stderr,
# where only launchd's <env>.launchd.log would catch it.
#
# The Keychain is where the token lives; the TODOIST_API_KEY environment
# variable is only a fallback for a manual run.
TODOIST_API_KEY = _load_from_keychain()
if TODOIST_API_KEY is None:
    TODOIST_API_KEY = os.environ.get("TODOIST_API_KEY")

CHANNELS: list[Channel] = []
_CONFIG_ERROR: Exception | None = None
_LOG_LEVEL_NAME = "INFO"
try:
    _LOG_LEVEL_NAME, CHANNELS = parse_config(CONFIG_PATH.read_text())
except FileNotFoundError:
    _CONFIG_ERROR = ConfigError(
        f"{CONFIG_PATH} not found"
        + (" — config.env is from before 3.0: rebuild the environment (env-build) to migrate it"
           if (PATHS.data_dir / "config.env").exists() else "")
    )
except (tomllib.TOMLDecodeError, ConfigError) as e:
    _CONFIG_ERROR = ConfigError(f"{CONFIG_PATH}: {e}")

# INFO (default) is the terse "starting" + one-line summary per channel.
# DEBUG adds per-query/per-pair detail (bridge calls and their timing,
# conflict resolutions, individual archive/prune actions) plus httpx's own
# request logging — meant for a troubleshooting window, not routine use.
LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME)


def validate() -> None:
    if _CONFIG_ERROR is not None:
        raise _CONFIG_ERROR
    if not TODOIST_API_KEY:
        raise RuntimeError(
            "TODOIST_API_KEY is not set — store it in macOS Keychain "
            "(security add-generic-password -s todoist-sync -a default -U -w "
            "— trailing -w with no value prompts for the token instead of "
            "exposing it in the command line)"
        )
