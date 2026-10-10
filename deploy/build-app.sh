#!/bin/bash
# Builds the Swift helpers and assembles deploy/todoist-sync.app, the bundle
# both LaunchAgents run. Touches nothing outside the repo: no LaunchAgents,
# no PATH symlink. install.sh runs this; run it on its own to try the bundle
# by hand, e.g.:
#
#   open -n -W deploy/todoist-sync.app --args --dry-run
#
# Launched through LaunchServices (open, or launchd via the plist), the
# bundle is its own "responsible process" for macOS privacy checks, so the
# Reminders permission prompt names Todoist Sync — rather than whichever
# terminal happened to start it, which may not be allowed to ask at all.
#
# TODOIST_SYNC_PYTHON picks the interpreter the bundle runs (default
# .venv/bin/python), e.g. a devenv venv's.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${TODOIST_SYNC_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"

if [ ! -x "$PYTHON" ]; then
    echo "error: Python interpreter not found at $PYTHON" >&2
    exit 1
fi
if ! command -v swift >/dev/null 2>&1; then
    echo "error: swift not found — install Xcode Command Line Tools first:" >&2
    echo "  xcode-select --install" >&2
    exit 1
fi

echo "Building reminders-bridge (Swift/EventKit helper)..."
(cd "$PROJECT_ROOT/swift/reminders-bridge" && swift build -c release)
codesign -s - "$PROJECT_ROOT/swift/reminders-bridge/.build/release/reminders-bridge" 2>/dev/null || true

echo "Building wake-watcher (Swift/NSWorkspace helper)..."
(cd "$PROJECT_ROOT/swift/wake-watcher" && swift build -c release)

# Both the periodic sync wrapper and wake-watcher are bundled as two
# executables inside one Todoist Sync.app, sharing a single Info.plist
# identity, rather than as bare binaries or two separate .apps. wake-watcher
# no longer touches Reminders itself (it just asks launchd to run the sync
# job on demand — see main.swift), so it doesn't need its own identity;
# folding it into the same bundle as the sync tool means macOS's Login
# Items list and the Reminders-access TCC prompt both consistently show one
# thing: "Todoist Sync". Rebuilt fresh every time since ad-hoc signing ties
# the TCC grant to the binary's content hash anyway — a stable bundle ID
# doesn't avoid re-prompting after a code change, it just makes the prompt
# itself clearer.
SYNC_APP="$PROJECT_ROOT/deploy/todoist-sync.app"
rm -rf "$SYNC_APP"
mkdir -p "$SYNC_APP/Contents/MacOS" "$SYNC_APP/Contents/Resources"
cp "$PROJECT_ROOT/deploy/todoist-sync-Info.plist" "$SYNC_APP/Contents/Info.plist"
cp "$PROJECT_ROOT/deploy/todoist-sync.icns" "$SYNC_APP/Contents/Resources/todoist-sync.icns"
sed -e "s@__PYTHON__@$PYTHON@g" \
    "$PROJECT_ROOT/deploy/todoist-sync" > "$SYNC_APP/Contents/MacOS/todoist-sync"
chmod +x "$SYNC_APP/Contents/MacOS/todoist-sync"
cp "$PROJECT_ROOT/swift/wake-watcher/.build/release/wake-watcher" "$SYNC_APP/Contents/MacOS/wake-watcher"
codesign -s - --force "$SYNC_APP" 2>/dev/null || true

echo "Built: $SYNC_APP (runs $PYTHON)"
