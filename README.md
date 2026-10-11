# todoist-sync

Two-way sync between a dedicated Apple Reminders list and a dedicated Todoist
project on macOS. Loosely based on the approach described in
[Syncing Apple Reminders to Todoist (two-way)](https://techresolve.blog/2026/03/03/syncing-apple-reminders-to-todoist-two-way/),
reworked after that approach (tagging notes/descriptions with a `sync_id`)
turned out to clutter every synced item's notes field.

## How it works

- An environment syncs one or more **channels**, each a Reminders list kept
  in sync with a Todoist project, listed in its `config.toml` (see
  `config.example.toml`). Only items in those containers are ever touched.
  Each channel can override the settings below; a Groceries list, for
  instance, turns `sections` off.
- Each channel's pairings are tracked in its own state file
  (`state/<list>.json` in the environment's data folder), mapping
  each reminder's native id directly to its Todoist task id. Nothing is
  written into notes/descriptions to identify a pairing — those fields stay
  exactly as you typed them.
- On each run the script:
  1. Creates a linked Todoist task for any open reminder in the list that
     isn't paired yet (and vice versa), copying over the title, notes/description,
     due date, and completion status.
  2. For every already-linked pair, reconciles title, notes, completion
     status, and due date in both directions — comparing each side's
     current value against what was last synced (recorded on the pair),
     not against volatile "last modified" timestamps, so the script's own
     writes can't make a stale value look "newer" and win on the next run.
     If both sides changed a field to different values between two syncs,
     `conflict_winner` (`reminders` by default, or `todoist`) decides
     which one sticks. With `sections` on (the default), board columns and
     list sections are reconciled the same way, matched by name.
  3. Moves any pair that's been completed on both sides for longer than
     `archive_after_days` (180 by default) into an archive section of the
     channel's state file. Archived pairs are no longer polled every run — that's
     what keeps sync fast as your history of completed tasks grows — but
     their ids are still remembered, so uncompleting one later resumes
     syncing it instead of creating a duplicate.
  4. Mirrors removals: once a reminder or task has been gone from its
     container for `prune_after_checks` consecutive runs (4 by
     default — about 20 minutes at prod's 5-minute sync interval), whether
     deleted or moved to another list/project, its counterpart is deleted
     from the other side and the pair is dropped from the state file. If more
     than 3 *open* items vanish from one side in the same run, nothing is
     deleted — that looks like a glitch (e.g. a full iCloud resync changing
     reminder ids) rather than tidying up — and the run fails, tripping the
     circuit breaker (below). Completed items don't count toward that limit.
- The Reminders side talks to EventKit through a small compiled Swift
  helper (`swift/reminders-bridge`), not AppleScript. AppleScript's
  Reminders support proved unreliable under testing — direct-by-name and
  direct-by-id addressing intermittently failed or returned wrong results.
  EventKit is Apple's proper, documented framework for Reminders and has
  been reliable in comparison; the Todoist side stays in Python since the
  `todoist-api-python` SDK already covers it well.

## When something goes wrong: the circuit breaker

Transient failures — offline, timeouts, Todoist rate limiting or server
errors — are retried within the run (after 10s, then 60s) and otherwise
left to the next scheduled run. Anything else (a bug, a revoked token,
lost Reminders access, a mass removal) **opens the circuit breaker**: the
error and traceback go to the log, the reason is written to
`circuit-breaker-open` in the environment's data folder, a Mac notification appears, and a
high-priority reminder lands in your default Reminders list so the phone
hears about it too. Every run after that just logs a warning and stops,
until you've looked, fixed the cause, and removed the file:

```bash
rm ~/"Library/Application Support/Todoist Sync/prod/circuit-breaker-open"
```

`--dry-run` ignores the breaker (it writes nothing), which makes it the
tool for seeing what tripped it.

## Known limitations

- **A completion can be lost if you "clean up" too fast.** If you complete
  something in Reminders and then delete it via "clean up completed"
  *before* the next sync runs, the completion is lost — once the reminder
  is gone there's no window left where it can be read as "completed," so
  the Todoist task stays open. This is inherent to polling-based sync
  (nothing watches for changes in real time) and isn't fixable without a
  different architecture. Un-completing itself (without deleting) does
  propagate correctly in either direction.
- **No true recurrence.** Todoist's API only exposes recurrence as a
  human-readable string (e.g. "Every! 1 weeks Saturday"), not a structured
  rule — confirmed against the official API reference, which documents no
  RRULE/frequency/interval fields anywhere. Todoist's own completion-based
  `every!` recurrence also has no EventKit equivalent at all (EventKit
  recurrence is always fixed-schedule, never "N units after actual
  completion"). Only the next due date keeps getting updated to match,
  each time the sync runs — there's no attempt to translate recurrence
  into a real EventKit recurrence rule.
- **Completion status isn't synced for recurring Todoist tasks, on
  purpose.** Verified directly: completing a recurring Todoist task never
  sets `completed_at` at all — it just advances `due` to the next
  occurrence and stays active forever. Reconciling "completed" for these
  would record a bogus baseline (since Todoist's side can never actually
  become "completed") that flips back and silently un-completes the
  reminder on the next run. So completion sync is skipped entirely for
  any pair whose Todoist task is recurring; the due-date advancing each
  cycle is what represents progress on those instead. Completing the
  reminder side of a recurring pair just marks the reminder done locally
  — it won't push anything to Todoist.
- **Location-based reminders aren't supported.** The Swift bridge only
  ever reads/writes `EKReminder.dueDateComponents` — it has no handling
  of `EKAlarm`/`structuredLocation` at all, and the Todoist task API this
  tool uses has no location field to put it in either way. A
  "remind me when I arrive/leave" reminder still syncs over as a plain
  task (title and notes carry across), but the location trigger itself is
  silently dropped.

## Layout

```
src/todoist_sync/          Python package — Todoist API, sync logic, state, CLI entry point
swift/reminders-bridge/    Swift/EventKit helper the Python side shells out to for all Reminders access
swift/wake-watcher/        Swift/NSWorkspace helper that fires a sync shortly after the Mac wakes
deploy/                    env.sh (builds an environment), Info.plist and LaunchAgent templates
integration_tests/         end-to-end scenarios against the real "Sync Test" pair, run as the staging app
```

## Environments (this fork)

This fork runs as separate, self-contained environments, so work in
progress can't touch a list you care about:

| | staging | prod |
|---|---|---|
| App | `~/Applications/Todoist Sync (Staging).app` | `~/Applications/Todoist Sync.app` |
| Code | the repo's live working tree | a git worktree at a tag, next to the repo (`<repo>-prod`) |
| Data (config.toml, state/, breaker) | `~/Library/Application Support/Todoist Sync/staging/` | `…/prod/` |
| Log (visible in Console.app) | `~/Library/Logs/Todoist Sync/staging.log` | `…/prod.log` |

```bash
devenv shell -- env-build staging     # or: env-build prod [tag]  (default: v<VERSION>)
devenv shell -- env-sync staging      # one sync; add --dry-run to preview
devenv shell -- env-log staging       # follow the log
```

Prod runs in the background: `env-build prod` (re)loads two LaunchAgents,
`com.todoist-sync.prod.sync` (every 5 minutes, and at login) and
`com.todoist-sync.prod.wake-watcher` (a sync soon after each wake). Both
show up as "Todoist Sync" under System Settings → General → Login Items &
Extensions. A deploy waits for a running sync to finish rather than
killing it. Staging only syncs in the background when built with
`env-build staging --agents`, because the integration tests refuse to run
alongside it; a plain `env-build staging` removes its agents again.

Each app asks for Reminders access once, under its own name. The bundles
are signed with an Apple Development identity (auto-detected, or
`TODOIST_SYNC_SIGN_IDENTITY`), so macOS ties that permission to the app's
identity rather than its exact bytes, and rebuilding never re-prompts.
Prod deploys a tagged version — every VERSION bump is tagged — and rolls
back by deploying an earlier tag. Running the
sync without an environment (straight from a checkout) is refused.

## Logs

Each environment logs to its own rotating file (1 MB cap, 3 backups) in
`~/Library/Logs/Todoist Sync/`, which Console.app lists under Log Reports.
At `log_level = "INFO"` (prod) a run logs a "starting" line and a one-line
summary per channel; `DEBUG` (staging) adds every Reminders and Todoist call and the
reasoning behind each decision. Each line carries its level, so
`grep ERROR prod.log` pulls out just the failures. A run started from a
terminal also mirrors the log to the console.

The agents' own stderr goes to `<env>.launchd.log` next to it: one line
per wake from the wake-watcher, and any crash that happens before the sync
has set up its log.

### Sleep and wake

launchd's `StartInterval` timer doesn't fire while the Mac is asleep, and
its catch-up after a wake isn't prompt. The wake-watcher
(`swift/wake-watcher/`, inside the app bundle) closes that gap: it observes
`NSWorkspace.didWakeNotification`, then polls DNS resolution of
`api.todoist.com` once a second, since syncing straight away races Wi-Fi
reassociation. As soon as it resolves (or after 30 seconds at most) it
asks launchd to run the sync job (`launchctl kickstart`), rather than
spawning the sync itself, so launchd is the parent of every run and the
Reminders permission is always attributed to the app. It never passes
`-k`, so a sync already in flight is left alone.

A `SIGUSR1` makes the wake-watcher act as if the Mac just woke, so the wake
path can be checked on a Mac that never sleeps:

```bash
devenv shell -- env-wake-test prod    # staging needs: env-build staging --agents
```

It sends the signal through launchd and passes once a sync has started.
