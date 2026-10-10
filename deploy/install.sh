#!/bin/bash
# Installs/uninstalls/updates the todoist-sync LaunchAgent for the current user.
# Safe to re-run — reinstalls/reloads if already installed.
#
# Usage:
#   ./deploy/install.sh              install and load (also the rebuild/reload step below)
#   ./deploy/install.sh --setup      re-run the interactive settings prompts, then reinstall
#   ./deploy/install.sh --update     pull latest code from git; run again afterward to rebuild/reload
#   ./deploy/install.sh --upgrade    explicit alias for the default install/reload path
#   ./deploy/install.sh --uninstall  unload and remove
#   ./deploy/install.sh --ensurepath  add ~/.local/bin to your shell's PATH
#   ./deploy/install.sh --ensurepath --all-shells  add to all detected shells
#
# --update and --upgrade are deliberately separate: --update only ever pulls
# code, it never touches requirements.txt's pinned dependency versions —
# those are pinned on purpose and only change when someone edits the file
# by hand.
#
# --ensurepath modifies shell config files (~/.zshrc, ~/.bash_profile,
# ~/.config/fish/config.fish) to add ~/.local/bin to PATH. By default it
# targets only the login shell ($SHELL); --all-shells targets every shell
# whose config file exists (or creates one if missing). Idempotent via a
# marker comment so it's safe to re-run.
set -euo pipefail

# This fork builds self-contained staging/prod environments with
# deploy/env.sh instead; the LaunchAgent setup below is being reworked to
# match and isn't usable meanwhile.
echo "install.sh is superseded by deploy/env.sh (staging|prod) in this fork; launchd setup is being reworked." >&2
exit 1

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.todoist-sync.local"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
WAKE_LABEL="com.todoist-sync.wake-watcher.local"
WAKE_PLIST_PATH="$HOME/Library/LaunchAgents/${WAKE_LABEL}.plist"

RUN_SETUP=0
case "${1:-}" in
    --ensurepath)
        LOCAL_BIN="$HOME/.local/bin"
        ALL_SHELLS=0
        if [ "${2:-}" = "--all-shells" ]; then
            ALL_SHELLS=1
        fi

        # Check if already on PATH
        case ":$PATH:" in
            *":$LOCAL_BIN:"*)
                echo "$LOCAL_BIN is already on your PATH."
                exit 0
                ;;
        esac

        MARKER="# Added by todoist-sync install.sh"
        ADDED=0

        add_to_config() {
            local config_file="$1" line="$2" shell_label="$3"
            if [ -f "$config_file" ] && grep -qF "$MARKER" "$config_file" 2>/dev/null; then
                echo "  $shell_label: already in $config_file — skipping."
                return 0
            fi
            mkdir -p "$(dirname "$config_file")"
            {
                echo ""
                echo "$MARKER"
                echo "$line"
            } >> "$config_file"
            echo "  $shell_label: added to $config_file"
            ADDED=1
        }

        echo "Adding $LOCAL_BIN to PATH..."

        if [ "$ALL_SHELLS" = "1" ]; then
            add_to_config "$HOME/.config/fish/config.fish" \
                "fish_add_path $LOCAL_BIN" "fish"
            add_to_config "$HOME/.zshrc" \
                "export PATH=\"$LOCAL_BIN:\$PATH\"" "zsh"
            add_to_config "$HOME/.bash_profile" \
                "export PATH=\"$LOCAL_BIN:\$PATH\"" "bash"
        else
            case "$SHELL" in
                */fish)
                    add_to_config "$HOME/.config/fish/config.fish" \
                        "fish_add_path $LOCAL_BIN" "fish"
                    ;;
                */zsh)
                    add_to_config "$HOME/.zshrc" \
                        "export PATH=\"$LOCAL_BIN:\$PATH\"" "zsh"
                    ;;
                */bash)
                    add_to_config "$HOME/.bash_profile" \
                        "export PATH=\"$LOCAL_BIN:\$PATH\"" "bash"
                    ;;
                *)
                    echo "Unknown shell: $SHELL" >&2
                    echo "Try: $0 --ensurepath --all-shells" >&2
                    echo "Or add $LOCAL_BIN to your shell's PATH manually." >&2
                    exit 1
                    ;;
            esac
        fi

        if [ "$ADDED" = "1" ]; then
            echo
            echo "Done! Open a new terminal or source your shell's config file"
            echo "for the PATH change to take effect."
        else
            echo
            echo "No changes needed."
        fi
        exit 0
        ;;
    --uninstall)
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        launchctl unload "$WAKE_PLIST_PATH" 2>/dev/null || true
        rm -f "$WAKE_PLIST_PATH"
        rm -rf "$PROJECT_ROOT/deploy/todoist-sync.app"
        rm -f "$HOME/.local/bin/todoist-sync"
        echo "Uninstalled: $LABEL, $WAKE_LABEL"
        exit 0
        ;;
    --update)
        if [ -n "$(cd "$PROJECT_ROOT" && git status --porcelain)" ]; then
            echo "error: uncommitted changes in $PROJECT_ROOT — commit or stash before updating." >&2
            exit 1
        fi
        echo "Pulling latest code..."
        (cd "$PROJECT_ROOT" && git pull)
        echo "Code updated. Run ./deploy/install.sh (or --upgrade) to rebuild and reload with it."
        exit 0
        ;;
    --setup)
        RUN_SETUP=1
        ;;
    "" | --upgrade)
        ;;
    *)
        echo "error: unknown argument '${1}'" >&2
        echo "Usage: $0 [--setup|--update|--upgrade|--ensurepath|--uninstall]" >&2
        exit 1
        ;;
esac

if [ ! -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    echo "Creating virtualenv..."
    python3 -m venv "$PROJECT_ROOT/.venv"
fi

FIRST_INSTALL=0
if [ ! -f "$PROJECT_ROOT/config.env" ]; then
    FIRST_INSTALL=1
fi

# `read` returns non-zero on EOF (stdin ran out of input, e.g. a closed or
# already-drained pipe) — under `set -e` that would otherwise kill the
# whole script silently the moment it happens, with no indication of why.
# Wrap every interactive read in this so it fails with a clear message
# instead. Piped input with actual data (scripted/automated setup) still
# works fine either way; this only fires when there's truly nothing left
# to read.
require_read() {
    if ! read "$@"; then
        echo >&2
        echo "error: no input available for a required prompt — run this in an" >&2
        echo "interactive terminal (or pipe in enough answers if scripting it)." >&2
        exit 1
    fi
}

# Store a token in Keychain without ever putting it in argv. Process
# arguments are readable by other processes running as the same user (and
# are easy to catch in a trace), so `-w "$TOKEN"` exposes the secret for
# the lifetime of the call. Given `-w` with no value, `security` instead
# reads the password from stdin — twice, because it asks for
# confirmation — which keeps it off the command line entirely. Apple's own
# man page recommends this form ("Put at end of command to be prompted").
#
# security writes its prompts to stderr, so those are captured and only
# surfaced if the store actually failed.
store_token_in_keychain() {
    local err
    if ! err="$(printf '%s\n%s\n' "$1" "$1" \
        | /usr/bin/security add-generic-password -s todoist-sync -a default -U -w 2>&1 >/dev/null)"; then
        echo "error: failed to store the token in Keychain:" >&2
        echo "$err" >&2
        return 1
    fi
}

# The Todoist API token is read from macOS Keychain at runtime (see
# config.py).  config.env is still supported as a fallback — both for
# non-macOS dev and for anyone who hasn't migrated yet.
KEYCHAIN_TOKEN="$(/usr/bin/security find-generic-password -s todoist-sync -a default -w 2>/dev/null || true)"

if [ -n "$KEYCHAIN_TOKEN" ] && [ "$RUN_SETUP" = "1" ]; then
    echo "A Todoist API token is already stored in Keychain."
    require_read -r -s -p "Enter a new token to replace it, or press Enter to keep the current one: " NEW_TOKEN
    echo
    if [ -n "$NEW_TOKEN" ]; then
        store_token_in_keychain "$NEW_TOKEN"
        echo "Token updated in macOS Keychain."
    fi
elif [ -z "$KEYCHAIN_TOKEN" ]; then
    if [ -f "$PROJECT_ROOT/config.env" ] && grep -q '^TODOIST_API_KEY=".' "$PROJECT_ROOT/config.env" 2>/dev/null; then
        # Migrate the existing token from config.env into Keychain.
        TOKEN="$(grep '^TODOIST_API_KEY=' "$PROJECT_ROOT/config.env" | sed 's/^TODOIST_API_KEY="\(.*\)"$/\1/')"
        if [ -n "$TOKEN" ] && [ "$TOKEN" != "YOUR_API_KEY_HERE" ]; then
            store_token_in_keychain "$TOKEN"
            # Strip the plaintext copy now that Keychain holds it, rather
            # than asking the user to do it by hand — a request that's easy
            # to skip, leaving the token sitting in a second, weaker
            # location indefinitely. config.env is mode 0600, but unlike
            # the Keychain copy it also rides along into Time Machine and
            # any folder-level backup. Only runs after the store above
            # succeeded (set -e aborts otherwise), and the token stays
            # recoverable with:
            #   security find-generic-password -s todoist-sync -a default -w
            MIGRATE_TMP="$(mktemp)"
            sed -E 's@^TODOIST_API_KEY=.*@# TODOIST_API_KEY has been moved to the macOS Keychain by install.sh@' \
                "$PROJECT_ROOT/config.env" > "$MIGRATE_TMP"
            mv "$MIGRATE_TMP" "$PROJECT_ROOT/config.env"
            chmod 600 "$PROJECT_ROOT/config.env"
            echo "Migrated API token from config.env to macOS Keychain."
            echo "The plaintext copy in config.env has been removed."
        fi
    else
        echo "No Todoist API token found in Keychain or config.env."
        echo "Enter your Todoist API token (input hidden):"
        require_read -s TOKEN
        if [ -z "$TOKEN" ]; then
            echo "error: no token entered." >&2
            exit 1
        fi
        store_token_in_keychain "$TOKEN"
        echo "Token stored in macOS Keychain."
    fi
fi

if [ ! -f "$PROJECT_ROOT/config.env" ]; then
    echo "Copying config.env.example to config.env (for non-API settings)..."
    cp "$PROJECT_ROOT/config.env.example" "$PROJECT_ROOT/config.env"
fi

# The other settings are plain (non-secret) key/value pairs in config.env,
# so rather than requiring anyone to hand-edit that file, prompt for them
# interactively — on first install (there's nothing to default to yet
# otherwise) and again any time someone runs --setup to revisit them.
if [ "$FIRST_INSTALL" = "1" ] || [ "$RUN_SETUP" = "1" ]; then
    echo
    echo "Configure sync settings (press Enter to keep the current/default value):"

    prompt_setting() {
        local key="$1" prompt_text="$2" validate_re="$3"
        local current input escaped tmp
        current="$(grep "^${key}=" "$PROJECT_ROOT/config.env" | sed -E "s/^${key}=\"(.*)\"\$/\1/")"
        while true; do
            require_read -r -p "$prompt_text [$current]: " input
            input="${input:-$current}"
            if [ -z "$validate_re" ] || [[ "$input" =~ $validate_re ]]; then
                break
            fi
            echo "  invalid value, try again." >&2
        done
        # Escape characters that are special in a sed replacement (the "@"
        # delimiter itself, "&", and "\") so arbitrary input can't corrupt
        # the substitution or the rest of the file.
        escaped="$(printf '%s' "$input" | sed -e 's/[&\@]/\\&/g')"
        tmp="$(mktemp)"
        sed -E "s@^${key}=.*@${key}=\"${escaped}\"@" "$PROJECT_ROOT/config.env" > "$tmp"
        mv "$tmp" "$PROJECT_ROOT/config.env"
    }

    prompt_setting SYNC_LIST_NAME \
        "Reminders list / Todoist project name to sync" ""
    prompt_setting SYNC_CONFLICT_WINNER \
        "Conflict winner when both sides changed (reminders/todoist)" \
        "^(reminders|todoist)$"
    prompt_setting SYNC_ARCHIVE_AFTER_DAYS \
        "Archive completed pairs after this many days" \
        "^[0-9]+$"
    prompt_setting SYNC_PRUNE_MISSING_AFTER_CHECKS \
        "Drop pairs missing on one side after this many consecutive checks" \
        "^[0-9]+$"
    prompt_setting SYNC_LOG_LEVEL \
        "Log level (DEBUG/INFO/WARNING/ERROR)" \
        "^(DEBUG|INFO|WARNING|ERROR)$"
    echo
fi

echo "Installing Python dependencies..."
"$PROJECT_ROOT/.venv/bin/pip" install -r "$PROJECT_ROOT/requirements.txt"
"$PROJECT_ROOT/.venv/bin/pip" install -e "$PROJECT_ROOT" --no-deps

# Swift helpers + deploy/todoist-sync.app (see build-app.sh for why the
# LaunchAgents run a bundle rather than bare binaries).
TODOIST_SYNC_PYTHON="$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/deploy/build-app.sh"
SYNC_APP="$PROJECT_ROOT/deploy/todoist-sync.app"

# Symlink a `todoist-sync` command onto PATH for easy manual runs (live
# console output, same as running the Python module directly) — points at
# the bundled executable so it always reflects the latest install.
mkdir -p "$HOME/.local/bin"
ln -sf "$SYNC_APP/Contents/MacOS/todoist-sync" "$HOME/.local/bin/todoist-sync"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        # Point the user at --ensurepath rather than printing raw
        # shell-specific commands — it handles shell detection and
        # config-file modification itself.
        echo "Note: $HOME/.local/bin isn't on your PATH yet — add it to run"
        echo "'todoist-sync' from anywhere."
        echo
        echo "  $0 --ensurepath"
        echo "  (add --all-shells to modify all detected shells)"
        echo
        echo "  Then start a new terminal."
        ;;
esac

mkdir -p "$PROJECT_ROOT/var"

sed \
    -e "s@__PROJECT_ROOT__@$PROJECT_ROOT@g" \
    -e "s@__LABEL__@$LABEL@g" \
    "$PROJECT_ROOT/deploy/com.todoist-sync.plist.template" > "$PLIST_PATH"

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

# Wake trigger: StartInterval alone only catches up some time after the Mac
# wakes, not immediately. wake-watcher is a small always-running LaunchAgent
# that observes NSWorkspace.didWakeNotification and, once the network is
# back, asks launchd to run the timer job above on demand (rather than
# spawning the sync itself) — see main.swift for why that matters for TCC
# attribution.
sed \
    -e "s@__PROJECT_ROOT__@$PROJECT_ROOT@g" \
    -e "s@__LABEL__@$WAKE_LABEL@g" \
    -e "s@__SYNC_LABEL__@$LABEL@g" \
    "$PROJECT_ROOT/deploy/com.todoist-sync.wake-watcher.plist.template" > "$WAKE_PLIST_PATH"

launchctl unload "$WAKE_PLIST_PATH" 2>/dev/null || true
launchctl load "$WAKE_PLIST_PATH"

echo "Installed and loaded: $LABEL (runs every 15 minutes)"
echo "Installed and loaded: $WAKE_LABEL (syncs ~10s after wake from sleep)"
echo "Logs: $PROJECT_ROOT/var/sync.log"
echo "Run a sync manually anytime with: todoist-sync"
echo "To uninstall: ./deploy/install.sh --uninstall"
echo
echo "Note: the first sync run will prompt macOS for Reminders access —"
echo "approve it in System Settings > Privacy & Security > Reminders."
