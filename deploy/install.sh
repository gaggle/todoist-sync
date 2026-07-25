#!/bin/bash
# Installs/uninstalls/updates the todoist-sync LaunchAgent for the current user.
# Safe to re-run — reinstalls/reloads if already installed.
#
# Usage:
#   ./deploy/install.sh              install and load (also the rebuild/reload step below)
#   ./deploy/install.sh --update     pull latest code from git; run again afterward to rebuild/reload
#   ./deploy/install.sh --upgrade    explicit alias for the default install/reload path
#   ./deploy/install.sh --uninstall  unload and remove
#
# --update and --upgrade are deliberately separate: --update only ever pulls
# code, it never touches requirements.txt's pinned dependency versions —
# those are pinned on purpose and only change when someone edits the file
# by hand.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.todoist-sync.local"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
WAKE_LABEL="com.todoist-sync.wake-watcher.local"
WAKE_PLIST_PATH="$HOME/Library/LaunchAgents/${WAKE_LABEL}.plist"

case "${1:-}" in
    --uninstall)
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        launchctl unload "$WAKE_PLIST_PATH" 2>/dev/null || true
        rm -f "$WAKE_PLIST_PATH"
        rm -rf "$PROJECT_ROOT/deploy/todoist-sync.app"
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
    "" | --upgrade)
        ;;
    *)
        echo "error: unknown argument '${1}'" >&2
        echo "Usage: $0 [--update|--upgrade|--uninstall]" >&2
        exit 1
        ;;
esac

if [ ! -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    echo "Creating virtualenv..."
    python3 -m venv "$PROJECT_ROOT/.venv"
fi

# The Todoist API token is read from macOS Keychain at runtime (see
# config.py).  config.env is still supported as a fallback — both for
# non-macOS dev and for anyone who hasn't migrated yet.
KEYCHAIN_TOKEN="$(security find-generic-password -s todoist-sync -a default -w 2>/dev/null || true)"

if [ -z "$KEYCHAIN_TOKEN" ]; then
    if [ -f "$PROJECT_ROOT/config.env" ] && grep -q '^TODOIST_API_KEY=".' "$PROJECT_ROOT/config.env" 2>/dev/null; then
        # Migrate the existing token from config.env into Keychain.
        TOKEN="$(grep '^TODOIST_API_KEY=' "$PROJECT_ROOT/config.env" | sed 's/^TODOIST_API_KEY="\(.*\)"$/\1/')"
        if [ -n "$TOKEN" ] && [ "$TOKEN" != "YOUR_API_KEY_HERE" ]; then
            security add-generic-password -s todoist-sync -a default -w "$TOKEN" -U
            echo "Migrated API token from config.env to macOS Keychain."
            echo "You can now remove the TODOIST_API_KEY line from config.env."
        fi
    else
        echo "No Todoist API token found in Keychain or config.env."
        echo "Enter your Todoist API token (input hidden):"
        read -s TOKEN
        if [ -z "$TOKEN" ]; then
            echo "error: no token entered." >&2
            exit 1
        fi
        security add-generic-password -s todoist-sync -a default -w "$TOKEN" -U
        echo "Token stored in macOS Keychain."
    fi
fi

if [ ! -f "$PROJECT_ROOT/config.env" ]; then
    echo "Copying config.env.example to config.env (for non-API settings)..."
    cp "$PROJECT_ROOT/config.env.example" "$PROJECT_ROOT/config.env"
fi

if ! command -v swift >/dev/null 2>&1; then
    echo "error: swift not found — install Xcode Command Line Tools first:" >&2
    echo "  xcode-select --install" >&2
    exit 1
fi

echo "Installing Python dependencies..."
"$PROJECT_ROOT/.venv/bin/pip" install -r "$PROJECT_ROOT/requirements.txt"
"$PROJECT_ROOT/.venv/bin/pip" install -e "$PROJECT_ROOT" --no-deps

echo "Building reminders-bridge (Swift/EventKit helper)..."
(cd "$PROJECT_ROOT/swift/reminders-bridge" && swift build -c release)
codesign -s - "$PROJECT_ROOT/swift/reminders-bridge/.build/release/reminders-bridge" 2>/dev/null || true

echo "Building wake-watcher (Swift/NSWorkspace helper)..."
(cd "$PROJECT_ROOT/swift/wake-watcher" && swift build -c release)

chmod +x "$PROJECT_ROOT/deploy/todoist-sync"

# Both the periodic sync wrapper and wake-watcher are bundled as two
# executables inside one Todoist Sync.app, sharing a single Info.plist
# identity, rather than as bare binaries or two separate .apps. wake-watcher
# no longer touches Reminders itself (it just asks launchd to run the sync
# job below on demand — see main.swift), so it doesn't need its own
# identity; folding it into the same bundle as the sync tool means macOS's
# Login Items list and the Reminders-access TCC prompt both consistently
# show one thing: "Todoist Sync". Rebuilt fresh on every install since
# ad-hoc signing ties the TCC grant to the binary's content hash anyway —
# a stable bundle ID doesn't avoid re-prompting after a code change, it
# just makes the prompt itself clearer.
SYNC_APP="$PROJECT_ROOT/deploy/todoist-sync.app"
rm -rf "$SYNC_APP"
mkdir -p "$SYNC_APP/Contents/MacOS" "$SYNC_APP/Contents/Resources"
cp "$PROJECT_ROOT/deploy/todoist-sync-Info.plist" "$SYNC_APP/Contents/Info.plist"
cp "$PROJECT_ROOT/deploy/todoist-sync.icns" "$SYNC_APP/Contents/Resources/todoist-sync.icns"
sed -e "s@__PROJECT_ROOT__@$PROJECT_ROOT@g" \
    "$PROJECT_ROOT/deploy/todoist-sync" > "$SYNC_APP/Contents/MacOS/todoist-sync"
chmod +x "$SYNC_APP/Contents/MacOS/todoist-sync"
cp "$PROJECT_ROOT/swift/wake-watcher/.build/release/wake-watcher" "$SYNC_APP/Contents/MacOS/wake-watcher"
codesign -s - --force "$SYNC_APP" 2>/dev/null || true

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
echo "To uninstall: ./deploy/install.sh --uninstall"
echo
echo "Note: the first sync run will prompt macOS for Reminders access —"
echo "approve it in System Settings > Privacy & Security > Reminders."
