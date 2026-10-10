#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import logging
import logging.handlers
import subprocess
import sys
from datetime import datetime, time, timedelta
from time import sleep as _sleep

import httpx

from . import config, state
from . import __version__
from .config import (
    ARCHIVE_AFTER_DAYS,
    BREAKER_PATH,
    CONFLICT_WINNER,
    CREATE_TODOIST_PROJECT,
    ENVIRONMENT,
    LOCK_PATH,
    LOG_LEVEL,
    LOG_PATH,
    PRUNE_MISSING_AFTER_CHECKS,
    REMINDERS_LIST,
    STATE_PATH,
    TODOIST_API_KEY,
    TODOIST_PROJECT,
)
from .dry_run import DryRunReminders, DryRunTodoist, is_placeholder_pair
from .reminders_bridge import RemindersBridge, create_alert_reminder
from .todoist_bridge import TodoistBridge

# More open items than this confirmed gone from one side in a single run
# is treated as a glitch rather than a tidy-up; see step 4 in main().
_MAX_OPEN_DELETES_PER_RUN = 3
# Seconds to wait before each retry of a run that hit a transient error.
_RETRY_DELAYS = (10, 60)

_MAX_LOG_BYTES = 1_000_000  # 1 MB per file
_LOG_BACKUP_COUNT = 3


def _configure_logging() -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # Write directly to the log file (rotating, capped) rather than through
    # stdout/stderr + launchd's StandardOutPath/StandardErrorPath, which had
    # no size limit and would grow forever.
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUP_COUNT
    )
    handler.setFormatter(formatter)

    effective_level = LOG_LEVEL or logging.INFO
    root = logging.getLogger()
    root.setLevel(effective_level)
    root.addHandler(handler)

    # launchd runs this headless with nothing capturing stdout/stderr (the
    # plist sets no StandardOutPath/StandardErrorPath), so this only ever
    # shows up for someone running the script manually in a terminal —
    # otherwise a run's outcome is invisible unless you go tail the file.
    if sys.stderr.isatty():
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(console_handler)

    # httpx/httpcore log every request at INFO with no handler of their own,
    # so without this they'd propagate straight into root and flood the log
    # with one line per Todoist API call. Only let that through in DEBUG,
    # where it doubles as the per-query trace for the Todoist side.
    third_party_level = logging.DEBUG if effective_level <= logging.DEBUG else logging.WARNING
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(third_party_level)


_configure_logging()
log = logging.getLogger("sync_tasks")


def _notify_macos(title: str, message: str) -> None:
    # AppleScript string literals: backslash and double-quote need escaping.
    def _escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    # Absolute path rather than $PATH lookup — same reasoning as
    # config._SECURITY_BIN.
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False)


class MassDeleteError(RuntimeError):
    """More open items vanished from one side in a single run than a person
    plausibly removed by hand. Deliberately unhandled: it trips the circuit
    breaker, so a human looks before anything gets deleted."""


def _is_transient(exc: Exception) -> bool:
    """Worth retrying: no response at all (offline, DNS, timeout), rate
    limiting, or a server-side error. Anything else needs a human."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


def _describe(exc: Exception) -> str:
    # An HTTPStatusError's message is mostly its URL (task ids); the status
    # code is the useful, non-personal part. The body stays at DEBUG.
    if isinstance(exc, httpx.HTTPStatusError):
        log.debug("Response body: %s", exc.response.text[:200])
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}"


def _raise_alarm(reason: str) -> None:
    """Gets a stopped sync noticed: a Mac notification, plus a high-priority
    reminder in the default Reminders list so it reaches the phone too."""
    _notify_macos(f"Todoist Sync ({ENVIRONMENT}) stopped", f"{reason} — paused until you check {LOG_PATH}")
    try:
        create_alert_reminder(
            f"Todoist Sync ({ENVIRONMENT}) stopped — check the Mac",
            f"{reason}\n\nSyncing is paused. Details: {LOG_PATH}\nTo resume once fixed: rm {BREAKER_PATH}",
        )
    except Exception:
        # The failure may well be Reminders access itself; never let the
        # alarm mask the error that tripped the breaker.
        log.exception("Couldn't create the alert reminder")


def _trip_breaker(exc: Exception) -> None:
    reason = _describe(exc)
    BREAKER_PATH.write_text(f"{datetime.now().isoformat(timespec='seconds')} {reason}\n")
    log.error("Circuit breaker opened — syncing is paused until %s is removed", BREAKER_PATH)
    _raise_alarm(reason)


def _acquire_lock():
    """Prevents overlapping runs: the 15-minute timer and the wake-watcher
    are separate launchd jobs that can both invoke this script around the
    same time (e.g. the Mac wakes right as the timer also fires). Without
    this, two processes could read the same state.json, both create a task
    for the same new reminder, and race to write state.json back — silently
    duplicating items or losing pairs. flock is tied to the open fd, so it's
    released automatically even if the process crashes."""
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def _due_to_datetime(due) -> tuple[datetime, bool] | tuple[None, None]:
    if due is None:
        return None, None
    if isinstance(due.date, datetime):
        return due.date, False
    return datetime.combine(due.date, time.min), True


def _due_equal(a_due: datetime | None, a_all_day: bool, b_due: datetime | None, b_all_day: bool) -> bool:
    if a_due is None and b_due is None:
        return True
    if a_due is None or b_due is None:
        return False
    if a_all_day != b_all_day:
        return False
    if a_all_day:
        return a_due.date() == b_due.date()
    return a_due == b_due


def _serialize_due(due_dt: datetime | None) -> str | None:
    return due_dt.isoformat() if due_dt else None


def _deserialize_due(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _redact_for_log(field_name: str, value):
    # Notes/description content is the most likely place for genuinely
    # sensitive text — unlike a title (short, needed to tell items apart in
    # a trace), it's rarely needed to diagnose a sync bug, so DEBUG logs a
    # size instead of the raw text.
    if field_name == "body" and value:
        return f"<{len(value)} chars>"
    return value


def _reconcile_scalar(
    last_value, r_value, t_value, set_todoist, set_reminders, *, field_name="value", pair_label="?"
):
    """3-way merge for a simple equality-comparable field: propagate
    whichever side changed since last_value, or resolve a genuine conflict
    (both changed, to different values) per CONFLICT_WINNER. Returns
    (new_value, changed)."""
    r_changed = r_value != last_value
    t_changed = t_value != last_value

    if not r_changed and not t_changed:
        return last_value, False

    if r_changed and t_changed and r_value != t_value:
        winner = r_value if CONFLICT_WINNER == "reminders" else t_value
        # Both sides changed to different values: whichever wasn't chosen
        # gets silently overwritten. Worth a WARNING (not just DEBUG) since
        # that's a real edit being discarded, not routine propagation — but
        # the values themselves are personal content (titles/notes), so
        # keep those out of the default-visible line and behind DEBUG.
        log.warning(
            "Conflict on %s for pair %s: %s wins (SYNC_CONFLICT_WINNER)",
            field_name,
            pair_label,
            CONFLICT_WINNER,
        )
        log.debug(
            "Conflict detail for pair %s: reminders=%r, todoist=%r",
            pair_label,
            _redact_for_log(field_name, r_value),
            _redact_for_log(field_name, t_value),
        )
    elif r_changed:
        winner = r_value
        log.debug(
            "Pair %s: %s changed in Reminders -> %r, propagating to Todoist",
            pair_label,
            field_name,
            _redact_for_log(field_name, winner),
        )
    else:
        winner = t_value
        log.debug(
            "Pair %s: %s changed in Todoist -> %r, propagating to Reminders",
            pair_label,
            field_name,
            _redact_for_log(field_name, winner),
        )

    if winner != t_value:
        set_todoist(winner)
    if winner != r_value:
        set_reminders(winner)
    return winner, True


def _log_repeat_not_synced(kind: str, title: str, pattern: str) -> None:
    # Repeat patterns don't translate between the two (and EventKit has no
    # equivalent of Todoist's completion-based "every!"), so only the item
    # and its next due date sync. Logged once, when the item is first
    # copied, so it's known which items repeat on one side only.
    log.info("Repeating %s %r copied as a one-off; its repeat pattern isn't synced: %s", kind, title, pattern)


def _pair_label(pair: dict) -> str:
    return f"r={pair['reminder_id']}/t={pair['task_id']}"


def _skip_save_state(*_args) -> None:
    pass


def main(dry_run: bool = False) -> None:
    log.info(
        "todoist-sync v%s (%s) starting%s", __version__, ENVIRONMENT, " (dry run: nothing will be written)" if dry_run else ""
    )
    config.validate()
    log.debug(
        "config: reminders_list=%r todoist_project=%r conflict_winner=%r archive_after_days=%d "
        "prune_after_checks=%d",
        REMINDERS_LIST,
        TODOIST_PROJECT,
        CONFLICT_WINNER,
        ARCHIVE_AFTER_DAYS,
        PRUNE_MISSING_AFTER_CHECKS,
    )
    reminders = RemindersBridge(REMINDERS_LIST)
    todoist = TodoistBridge(TODOIST_API_KEY)
    project_id = todoist.get_or_create_project(TODOIST_PROJECT, create=CREATE_TODOIST_PROJECT and not dry_run)
    # A dry run reads both sides for real but routes every write — to
    # Reminders, Todoist, and state.json — to a log line instead.
    save_state = _skip_save_state if dry_run else state.save_state
    if dry_run:
        reminders = DryRunReminders(reminders)
        todoist = DryRunTodoist(todoist)

    pairs, archive = state.load_state(STATE_PATH)
    linked_reminder_ids = {p["reminder_id"] for p in pairs}
    linked_task_ids = {p["task_id"] for p in pairs}
    # Archived pairs (completed on both sides for SYNC_ARCHIVE_AFTER_DAYS)
    # are excluded from the per-pair reconciliation loop below, but their
    # ids are kept here so a reminder/task that gets reactivated is matched
    # back to its existing pair instead of spawning a duplicate.
    archived_by_reminder = {p["reminder_id"]: p for p in archive}
    archived_by_task = {p["task_id"]: p for p in archive}

    def _unarchive(pair: dict) -> None:
        log.debug("Pair %s: reactivated from archive", _pair_label(pair))
        archive.remove(pair)
        del archived_by_reminder[pair["reminder_id"]]
        del archived_by_task[pair["task_id"]]
        pairs.append(pair)
        linked_reminder_ids.add(pair["reminder_id"])
        linked_task_ids.add(pair["task_id"])

    # 1. Propagate brand-new items sitting in the dedicated containers,
    #    including whatever due date/time each one already has. The pair's
    #    fields record what we just synced, so reconciliation below has a
    #    correct baseline from the start.
    created_in_todoist = 0
    reactivated = 0
    for r in reminders.get_reminders():
        if r["id"] in linked_reminder_ids:
            continue
        archived_pair = archived_by_reminder.get(r["id"])
        if archived_pair is not None:
            # get_reminders() returns every reminder regardless of
            # completion, so an archived id showing up here isn't
            # necessarily a reactivation — only act if it's been unchecked.
            if not r["completed"]:
                _unarchive(archived_pair)
                reactivated += 1
                save_state(STATE_PATH, pairs, archive)
            continue
        # Only open reminders get copied, mirroring the Todoist side
        # (get_active_tasks() never returns completed tasks). Nothing is
        # recorded for a skipped reminder, so one that's reopened later is
        # simply picked up then. Completed ones are mostly the
        # per-occurrence history Reminders keeps for repeating reminders.
        if r["completed"]:
            continue
        if r["recurrence"]:
            _log_repeat_not_synced("reminder", r["name"], r["recurrence"])
        task_id = todoist.create_task(project_id, r["name"], r["body"], r["due"], r["all_day"])
        pairs.append(
            {
                "reminder_id": r["id"],
                "task_id": task_id,
                "due": _serialize_due(r["due"]),
                "all_day": r["all_day"],
                "completed": False,
                "name": r["name"],
                "body": r["body"],
            }
        )
        linked_task_ids.add(task_id)
        created_in_todoist += 1
        # Persist immediately: create_task() is not idempotent, so if a
        # later item in this loop fails (e.g. connectivity drops mid-run),
        # an un-persisted pair here would make this reminder look unlinked
        # again next run and get a duplicate task created for it.
        save_state(STATE_PATH, pairs, archive)

    created_in_reminders = 0
    for t in todoist.get_active_tasks(project_id):
        if t.id in linked_task_ids:
            continue
        archived_pair = archived_by_task.get(t.id)
        if archived_pair is not None:
            # get_active_tasks() only ever returns open tasks, so a match
            # here is always a genuine reactivation — no completed check
            # needed, unlike the reminders side above.
            _unarchive(archived_pair)
            reactivated += 1
            save_state(STATE_PATH, pairs, archive)
            continue
        if t.due is not None and t.due.is_recurring:
            _log_repeat_not_synced("Todoist task", t.content, t.due.string)
        due_dt, all_day = _due_to_datetime(t.due)
        body = t.description or ""
        reminder_id = reminders.create_reminder(t.content, body, due_dt, all_day)
        pairs.append(
            {
                "reminder_id": reminder_id,
                "task_id": t.id,
                "due": _serialize_due(due_dt),
                "all_day": all_day,
                "completed": False,
                "name": t.content,
                "body": body,
            }
        )
        linked_reminder_ids.add(reminder_id)
        created_in_reminders += 1
        # Same rationale as above: create_reminder() is not idempotent.
        save_state(STATE_PATH, pairs, archive)

    # 2. Reconcile already-linked pairs by id directly. Reminder lookups are
    #    scoped to REMINDERS_LIST on the Swift side (not just addressed by global
    #    id) so a tampered or stale state.json entry can't read/mutate a
    #    reminder outside the intended list. Every field is resolved against
    #    the last-synced value recorded on the pair (not wall-clock "last
    #    modified" timestamps) — otherwise our own writes to one side make
    #    that side look "newer" on the next run and its value keeps winning
    #    even when it's the stale one. Genuine conflicts (both sides changed
    #    to different values since the last sync) resolve per
    #    CONFLICT_WINNER.
    completed_synced = 0
    due_synced = 0
    name_synced = 0
    notes_synced = 0
    now = datetime.now()
    to_archive = []
    to_prune = []
    # One listing of the whole list rather than a bridge process per pair;
    # taken after step 1, so it includes the reminders just created there.
    current_reminders = {r["id"]: r for r in reminders.get_reminders()}
    for pair in pairs:
        if dry_run and is_placeholder_pair(pair):
            continue
        r = current_reminders.get(pair["reminder_id"])
        t = todoist.get_task(pair["task_id"], project_id)
        if r is None or t is None:
            # A reminder missing from its list's listing, or get_task's None
            # on a confirmed 404, is never a transient error (those raise
            # instead) — so this is a real
            # deletion signal, not a flaky one. Still require a few
            # consecutive misses rather than pruning on the first sighting,
            # in case the other side is mid-edit (e.g. an EventKit/iCloud
            # propagation lag) rather than genuinely gone. A check count
            # (not elapsed time) is what actually matters here — it demands
            # that many independent confirmations regardless of how much
            # wall-clock time passed between them (e.g. the Mac sleeping).
            # Once confirmed gone, the removal is mirrored and the pair
            # pruned (step 4).
            pair["missing_checks"] = pair.get("missing_checks", 0) + 1
            log.debug(
                "Pair %s: reminder or task missing (check %d/%d)",
                _pair_label(pair),
                pair["missing_checks"],
                PRUNE_MISSING_AFTER_CHECKS,
            )
            if pair["missing_checks"] >= PRUNE_MISSING_AFTER_CHECKS:
                # The side still holding the item, if any, is where the
                # removal gets mirrored.
                survivor = "reminder" if r is not None else "task" if t is not None else None
                to_prune.append((pair, survivor))
            continue
        if pair.get("missing_checks"):
            pair["missing_checks"] = 0

        # Recurring Todoist tasks never actually get completed_at set —
        # "completing" one just advances its due date to the next
        # occurrence and leaves it structurally incomplete. Treating that
        # as a real completion signal would record a bogus "completed"
        # baseline that the next run reads as Todoist having reverted,
        # silently un-completing the reminder. Due-date reconciliation
        # below is what actually represents progress on these. They're
        # also never eligible for archiving, since they never reach a
        # real "done" state.
        is_recurring = t.due is not None and t.due.is_recurring
        if not is_recurring:
            new_completed, completed_changed = _reconcile_scalar(
                pair.get("completed", False),
                r["completed"],
                t.completed_at is not None,
                set_todoist=lambda v: (todoist.complete_task(t.id) if v else todoist.uncomplete_task(t.id)),
                set_reminders=lambda v: (
                    reminders.complete_reminder(r["id"]) if v else reminders.uncomplete_reminder(r["id"])
                ),
                field_name="completed",
                pair_label=_pair_label(pair),
            )
            pair["completed"] = new_completed
            if completed_changed:
                completed_synced += 1
            # completed_at anchors the SYNC_ARCHIVE_AFTER_DAYS grace period.
            # Stamp it on every fresh transition to completed, and backfill
            # it for pairs that were already completed before this field
            # existed, so they don't sit un-archived forever.
            if new_completed:
                if completed_changed or not pair.get("completed_at"):
                    pair["completed_at"] = now.isoformat()
            else:
                pair["completed_at"] = None

        new_name, name_changed = _reconcile_scalar(
            pair.get("name", r["name"]),
            r["name"],
            t.content,
            set_todoist=lambda v: todoist.set_task_content(t.id, v),
            set_reminders=lambda v: reminders.set_name(r["id"], v),
            field_name="name",
            pair_label=_pair_label(pair),
        )
        pair["name"] = new_name
        if name_changed:
            name_synced += 1

        new_body, body_changed = _reconcile_scalar(
            pair.get("body", r["body"]),
            r["body"],
            t.description or "",
            set_todoist=lambda v: todoist.set_task_description(t.id, v),
            set_reminders=lambda v: reminders.set_body(r["id"], v),
            field_name="body",
            pair_label=_pair_label(pair),
        )
        pair["body"] = new_body
        if body_changed:
            notes_synced += 1

        last_due = _deserialize_due(pair.get("due"))
        last_all_day = pair.get("all_day", False)
        r_due, r_all_day = r["due"], r["all_day"]
        t_due, t_all_day = _due_to_datetime(t.due)

        r_changed = not _due_equal(r_due, r_all_day, last_due, last_all_day)
        t_changed = not _due_equal(t_due, t_all_day, last_due, last_all_day)

        if r_changed and t_changed and not _due_equal(r_due, r_all_day, t_due, t_all_day):
            # Both sides changed to different values since the last sync —
            # genuine conflict, resolved per CONFLICT_WINNER.
            log.warning(
                "Conflict on due date for pair %s: %s wins (SYNC_CONFLICT_WINNER)",
                _pair_label(pair),
                CONFLICT_WINNER,
            )
            log.debug(
                "Conflict detail for pair %s: reminders=%r, todoist=%r", _pair_label(pair), r_due, t_due
            )
            if CONFLICT_WINNER == "todoist":
                winning_due, winning_all_day = t_due, t_all_day
                if t_due is not None:
                    reminders.set_due_date(r["id"], t_due, t_all_day)
                    due_synced += 1
            else:
                winning_due, winning_all_day = r_due, r_all_day
                if r_due is not None:
                    todoist.set_task_due(t.id, r_due, r_all_day)
                    due_synced += 1
        elif r_changed and r_due is not None:
            log.debug("Pair %s: due date changed in Reminders -> %r, propagating to Todoist", _pair_label(pair), r_due)
            todoist.set_task_due(t.id, r_due, r_all_day)
            due_synced += 1
            winning_due, winning_all_day = r_due, r_all_day
        elif t_changed and t_due is not None:
            log.debug("Pair %s: due date changed in Todoist -> %r, propagating to Reminders", _pair_label(pair), t_due)
            reminders.set_due_date(r["id"], t_due, t_all_day)
            due_synced += 1
            winning_due, winning_all_day = t_due, t_all_day
        else:
            winning_due, winning_all_day = last_due, last_all_day

        pair["due"] = _serialize_due(winning_due)
        pair["all_day"] = winning_all_day

        if (
            not is_recurring
            and pair.get("completed")
            and pair.get("completed_at")
            and now - datetime.fromisoformat(pair["completed_at"]) >= timedelta(days=ARCHIVE_AFTER_DAYS)
        ):
            to_archive.append(pair)

    # 3. Move pairs that have sat completed on both sides past the grace
    #    period into the archive. Archived pairs are excluded from the
    #    per-pair API reconciliation above on future runs — the ids are
    #    kept only so a later reactivation (see step 1) is recognized
    #    instead of spawning a duplicate.
    for pair in to_archive:
        log.debug("Pair %s: archived (completed on both sides past SYNC_ARCHIVE_AFTER_DAYS)", _pair_label(pair))
        pairs.remove(pair)
        archive.append(pair)

    # 4. Mirror confirmed removals, then drop those pairs. An item gone from
    #    one side for PRUNE_MISSING_AFTER_CHECKS runs — deleted, or moved to
    #    another list/project — is deleted from the other side too, so the
    #    two containers stay mirrors; a pair gone from both sides just drops.
    #    Pairs are removed outright, not archived: nothing is left to
    #    reactivate.
    #
    #    Brake: lots of *open* items vanishing at once looks less like
    #    tidying up than like a glitch — e.g. a full iCloud resync, which
    #    Apple documents can change every reminder's id — and mirroring
    #    that would wipe the shared project. Above the limit, nothing is
    #    deleted: MassDeleteError trips the circuit breaker so a human
    #    looks first. Completed items don't count: clearing out a
    #    ticked-off list is routine.
    open_removals = sum(1 for pair, survivor in to_prune if survivor and not pair.get("completed"))
    if open_removals > _MAX_OPEN_DELETES_PER_RUN:
        raise MassDeleteError(
            f"{open_removals} open items vanished from one side in a single run "
            f"(limit {_MAX_OPEN_DELETES_PER_RUN}) — nothing was deleted"
        )

    removals_mirrored = 0
    for pair, survivor in to_prune:
        if survivor is not None:
            # At INFO with the title: the log is the record of what was
            # deleted, should anything need recreating by hand.
            log.info(
                "%s removal of %r: deleting the %s",
                "DRY RUN: would mirror" if dry_run else "Mirroring",
                pair.get("name"),
                "reminder" if survivor == "reminder" else "Todoist task",
            )
            if survivor == "reminder":
                reminders.delete_reminder(pair["reminder_id"])
            else:
                todoist.delete_task(pair["task_id"])
            removals_mirrored += 1
        log.debug(
            "Pair %s: pruned (missing for %d consecutive checks; deleted the %s)",
            _pair_label(pair),
            pair["missing_checks"],
            survivor or "nothing, both sides gone",
        )
        pairs.remove(pair)

    save_state(STATE_PATH, pairs, archive)

    if not any(
        (
            created_in_todoist,
            created_in_reminders,
            completed_synced,
            due_synced,
            name_synced,
            notes_synced,
            reactivated,
            to_archive,
            to_prune,
        )
    ):
        log.info("%sSync complete: no changes", "DRY RUN: " if dry_run else "")
    else:
        log.info(
            "%sSync complete: %d reminder(s) -> Todoist, %d task(s) -> Reminders, "
            "%d completion(s) synced, %d due date(s) synced, %d title(s) synced, "
            "%d note(s) synced, %d reactivated from archive, %d archived, %d removal(s) mirrored, %d pruned",
            "DRY RUN: " if dry_run else "",
            created_in_todoist,
            created_in_reminders,
            completed_synced,
            due_synced,
            name_synced,
            notes_synced,
            reactivated,
            len(to_archive),
            removals_mirrored,
            len(to_prune),
        )

    todoist.close()


def run(argv: list[str] | None = None) -> int:
    """One scheduled (or manual) sync, guarded by a circuit breaker.

    The breaker is a file: absent means closed (sync runs), present means
    open — something unexpected happened and a human has to look before
    syncing resumes (remove the file to close it). Transient errors are
    retried with backoff and never trip it; anything else does, raises the
    alarm, and re-raises so the run crashes visibly. A dry run writes
    nothing, so it neither respects nor trips the breaker — it's the tool
    for diagnosing what tripped it.
    """
    parser = argparse.ArgumentParser(prog="todoist-sync")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read both sides and log what would change, without writing anything",
    )
    # parse_known_args: an app bundle launched via LaunchServices can be
    # handed extra arguments of its own, which shouldn't abort the sync.
    args, _ = parser.parse_known_args(argv)

    if ENVIRONMENT is None:
        log.error("Not running in an environment (TODOIST_SYNC_HOME is unset) — use a Todoist Sync app built by deploy/env.sh")
        return 2

    if not args.dry_run and BREAKER_PATH.exists():
        log.warning(
            "Circuit breaker is open (%s) — not syncing. Fix the cause, then resume with: rm %s",
            BREAKER_PATH.read_text().strip(),
            BREAKER_PATH,
        )
        return 1

    lock_file = _acquire_lock()
    if lock_file is None:
        log.info("Another sync is already running — skipping this run")
        return 0
    try:
        for delay in (*_RETRY_DELAYS, None):
            try:
                main(dry_run=args.dry_run)
                return 0
            except Exception as e:
                if not _is_transient(e):
                    raise
                if delay is None:
                    log.warning("Sync failed: %s, still after %d retries — leaving it to the next run", _describe(e), len(_RETRY_DELAYS))
                    return 1
                log.warning("Sync failed: %s — retrying in %ds", _describe(e), delay)
                _sleep(delay)
    except Exception as e:
        log.exception("Sync failed unexpectedly")
        if not args.dry_run:
            _trip_breaker(e)
        raise
    finally:
        lock_file.close()


if __name__ == "__main__":
    sys.exit(run())
