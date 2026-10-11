from __future__ import annotations


def test_reminder_moved_into_a_section_moves_its_task_into_a_same_named_one(bench, sync):
    reminder_id = bench.reminders.create_reminder("Sand the door", "", None)
    sync()

    bench.reminders.set_section(reminder_id, "Doing")
    sync()

    assert bench.task_section("Sand the door") == "Doing"


def test_task_moved_to_a_section_moves_its_reminder_into_a_same_named_one(bench, sync):
    bench.add_task("Oil the hinges")
    sync()

    bench.move_task("Oil the hinges", "Now")
    sync()

    assert bench.reminders_by_name()["Oil the hinges"]["section"] == "Now"


def test_task_created_in_a_section_lands_in_that_section_as_a_reminder(bench, sync):
    bench.add_task("Prime the wall")
    bench.move_task("Prime the wall", "Doing")

    sync()

    assert bench.reminders_by_name()["Prime the wall"]["section"] == "Doing"


def test_taking_a_task_out_of_its_section_takes_its_reminder_out_too(bench, sync):
    bench.add_task("Clean the brushes")
    bench.move_task("Clean the brushes", "Now")
    sync()
    assert bench.reminders_by_name()["Clean the brushes"]["section"] == "Now"

    bench.move_task("Clean the brushes", None)
    sync()

    assert bench.reminders_by_name()["Clean the brushes"]["section"] is None


def test_columns_settle_after_moves(bench, sync, caplog):
    reminder_id = bench.reminders.create_reminder("Tape the edges", "", None)
    sync()
    bench.reminders.set_section(reminder_id, "Now")
    sync()

    with caplog.at_level("INFO"):
        sync()

    assert "Sync complete (Sync Test ↔ Sync Test): no changes" in caplog.text


def test_a_reminder_taken_out_of_a_section_can_go_back_in(bench, sync):
    bench.add_task("Store the ladder")
    bench.move_task("Store the ladder", "Now")
    sync()
    bench.move_task("Store the ladder", None)
    sync()

    bench.move_task("Store the ladder", "Doing")
    sync()

    assert bench.reminders_by_name()["Store the ladder"]["section"] == "Doing"
