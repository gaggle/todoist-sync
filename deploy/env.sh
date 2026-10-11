#!/bin/bash
# Builds (or rebuilds) one environment of Todoist Sync:
#
#   deploy/env.sh staging [--agents]   runs the repo's live code — edit, then just run it
#   deploy/env.sh prod [tag]           runs a git worktree checked out at a tag
#                                      (default: the tag of the repo's current VERSION),
#                                      next to this checkout: <repo>-prod
#
# Prod always runs in the background: a LaunchAgent syncs every 5 minutes,
# and a second one (wake-watcher) starts a sync soon after the Mac wakes.
# Staging only gets them with --agents, since a background sync would
# interfere with the integration tests; any rebuild without it removes them.
#
# Each environment is self-contained, so iterating on staging can't touch
# what prod syncs:
#
#   ~/Applications/<name>.app                          app bundle (own Reminders permission)
#   ~/Library/Application Support/Todoist Sync/<env>/  config.env, state, lock, breaker
#   ~/Library/Logs/Todoist Sync/<env>.log              log (shows in Console.app)
#   ~/Library/Logs/Todoist Sync/<env>.launchd.log      the agents' stderr: wakes, early crashes
#   ~/Library/LaunchAgents/com.todoist-sync.<env>.{sync,wake-watcher}.plist
#
# The bundle holds Info.plist, an icon, a wrapper with fixed paths and the
# wake-watcher. It's signed with an Apple Development identity rather than
# ad-hoc: macOS then ties the Reminders permission to the app's identity
# (bundle id + certificate), not its exact bytes, so rebuilding never
# re-prompts; and the Team ID lets Login Items show the agents as the app,
# with its name and icon, instead of a bare executable.
#
# Prod deploys and rolls back by checking its worktree out at another tag;
# devenv gives that worktree its own venv (and keeps its interpreter rooted
# in the Nix store). The worktree can't live under Application Support:
# devenv's Python wrapper breaks on spaces in its path.
set -euo pipefail

ENV="${1:-}"
AGENTS=no
case "$ENV" in
    staging) NAME="Todoist Sync (Staging)"; BUNDLE_ID="com.todoist-sync.staging"
             case "${2:-}" in --agents) AGENTS=yes ;; "") ;; *) echo "usage: $0 staging [--agents]" >&2; exit 1 ;; esac ;;
    prod)    NAME="Todoist Sync";           BUNDLE_ID="com.todoist-sync.app"; AGENTS=yes ;;
    *) echo "usage: $0 staging [--agents] | prod [tag]" >&2; exit 1 ;;
esac

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$HOME/Library/Application Support/Todoist Sync/$ENV"
LOG="$HOME/Library/Logs/Todoist Sync/$ENV.log"
LAUNCHD_LOG="$HOME/Library/Logs/Todoist Sync/$ENV.launchd.log"
APP="$HOME/Applications/$NAME.app"
DOMAIN="gui/$(id -u)"
SYNC_LABEL="com.todoist-sync.$ENV.sync"
WAKE_LABEL="com.todoist-sync.$ENV.wake-watcher"
command -v swift >/dev/null || { echo "error: swift not found — xcode-select --install" >&2; exit 1; }
SIGN_ID="${TODOIST_SYNC_SIGN_IDENTITY:-$(security find-identity -v -p codesigning | awk -F'"' '/"Apple Development: /{print $2; exit}')}"
[ -n "$SIGN_ID" ] || { echo "error: no Apple Development signing identity — add your Apple ID in Xcode > Settings > Accounts, or set TODOIST_SYNC_SIGN_IDENTITY" >&2; exit 1; }

mkdir -p "$DATA" "$(dirname "$LOG")" "$HOME/Applications"
chmod 700 "$DATA"

# Stop the agents before touching the code and the bundle they run. A sync
# is never killed mid-run — between a Todoist write and the state save that
# would duplicate items on the next run — so wait until nothing holds the
# lock (a manual env-sync counts too), then unload. A timer firing in the
# instant between the two is stopped before it has written anything.
launchctl bootout "$DOMAIN/$WAKE_LABEL" 2>/dev/null || true
while lsof -t "$DATA/sync.lock" >/dev/null 2>&1; do
    echo "Waiting for the running sync to finish..."
    sleep 2
done
launchctl bootout "$DOMAIN/$SYNC_LABEL" 2>/dev/null || true
if [ "$AGENTS" = yes ]; then
    trap 'echo "error: $ENV is NOT syncing in the background until env-build $ENV succeeds" >&2' ERR
fi
rm -f "$HOME/Library/LaunchAgents/$SYNC_LABEL.plist" "$HOME/Library/LaunchAgents/$WAKE_LABEL.plist"

if [ "$ENV" = staging ]; then
    CODE="$REPO"
else
    TAG="${2:-v$(cat "$REPO/VERSION")}"
    if ! git -C "$REPO" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
        echo "error: no tag $TAG — prod only runs tagged versions (tag the VERSION bump first)" >&2
        exit 1
    fi
    CODE="$(dirname "$REPO")/$(basename "$REPO")-prod"
    if [ -e "$CODE/.git" ]; then
        git -C "$CODE" checkout -q --detach "$TAG"
    else
        git -C "$REPO" worktree add -q --detach "$CODE" "$TAG"
    fi
    echo "Prod worktree at $TAG ($(git -C "$CODE" rev-parse --short HEAD))"
    echo "Building its devenv..."
    (cd "$CODE" && devenv shell -- true)
fi
echo "Building the Reminders bridge and the wake-watcher..."
(cd "$CODE/swift/reminders-bridge" && swift build -c release)
(cd "$CODE/swift/wake-watcher" && swift build -c release)
PYTHON="$CODE/.devenv/state/venv/bin/python"
BRIDGE="$CODE/swift/reminders-bridge/.build/release/reminders-bridge"
[ -x "$PYTHON" ] || { echo "error: no devenv venv at $PYTHON — run 'devenv shell' in $CODE once" >&2; exit 1; }
codesign -s - --force "$BRIDGE" 2>/dev/null

if [ ! -f "$DATA/config.env" ]; then
    case "$ENV" in
        staging) PAIR=('SYNC_REMINDERS_LIST="Sync Test"' 'SYNC_TODOIST_PROJECT="Sync Test"' 'SYNC_LOG_LEVEL="DEBUG"') ;;
        prod)    PAIR=('SYNC_REMINDERS_LIST="Chores"' 'SYNC_TODOIST_PROJECT="Family Project"' 'SYNC_LOG_LEVEL="INFO"') ;;
    esac
    {
        echo "# Todoist Sync — $ENV. Other settings and their defaults: config.env.example in the repo."
        printf '%s\n' "${PAIR[@]}"
    } > "$DATA/config.env"
    chmod 600 "$DATA/config.env"
    echo "Wrote $DATA/config.env"
fi

echo "Assembling $APP..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sed -e "s|__NAME__|$NAME|g" -e "s|__BUNDLE_ID__|$BUNDLE_ID|g" \
    "$REPO/deploy/todoist-sync-Info.plist" > "$APP/Contents/Info.plist"
cp "$REPO/deploy/todoist-sync.icns" "$APP/Contents/Resources/todoist-sync.icns"
cp "$CODE/swift/wake-watcher/.build/release/wake-watcher" "$APP/Contents/MacOS/wake-watcher"
codesign -s "$SIGN_ID" --force "$APP/Contents/MacOS/wake-watcher"
{
    echo '#!/bin/bash'
    echo "# $NAME — generated by deploy/env.sh; rebuild rather than edit."
    echo "export TODOIST_SYNC_ENV=\"$ENV\""
    echo "export TODOIST_SYNC_HOME=\"$DATA\""
    echo "export TODOIST_SYNC_LOG_FILE=\"$LOG\""
    echo "export TODOIST_SYNC_BRIDGE=\"$BRIDGE\""
    if [ "$ENV" = staging ]; then
        # Staging only: run the integration tests as this app, so they get its
        # Reminders permission. The exit code goes to a file, since `open`
        # doesn't pass it on.
        echo 'if [ "${1:-}" = "--integration-tests" ]; then'
        echo '    shift'
        echo "    cd \"$REPO\""
        echo "    \"$PYTHON\" -m pytest integration_tests \"\$@\""
        echo "    echo \$? > \"$DATA/integration-tests.exit\""
        echo '    exit 0'
        echo 'fi'
    fi
    echo "exec \"$PYTHON\" -m todoist_sync.sync_tasks \"\$@\""
} > "$APP/Contents/MacOS/todoist-sync"
chmod +x "$APP/Contents/MacOS/todoist-sync"
codesign -s "$SIGN_ID" --force "$APP"

if [ "$AGENTS" = yes ]; then
    echo "Loading the agents..."
    for job in sync wake-watcher; do
        label="com.todoist-sync.$ENV.$job"
        plist="$HOME/Library/LaunchAgents/$label.plist"
        mkdir -p "$(dirname "$plist")"
        sed -e "s|__LABEL__|$label|g" -e "s|__APP__|$APP|g" -e "s|__BUNDLE_ID__|$BUNDLE_ID|g" \
            -e "s|__SYNC_LABEL__|$SYNC_LABEL|g" -e "s|__LAUNCHD_LOG__|$LAUNCHD_LOG|g" \
            "$REPO/deploy/launchd-$job.plist" > "$plist"
        plutil -lint -s "$plist"
        launchctl bootstrap "$DOMAIN" "$plist"   # RunAtLoad: the sync starts right away
    done
fi

echo
echo "$NAME ready."
echo "  sync:     open -n -W \"$APP\" [--args --dry-run]"
if [ "$AGENTS" = yes ]; then
    echo "  agents:   $SYNC_LABEL (every 5 min), $WAKE_LABEL"
    echo "            launchctl print $DOMAIN/$SYNC_LABEL"
fi
echo "  log:      tail -F \"$LOG\""
echo "  data:     $DATA"
