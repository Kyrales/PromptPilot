from promptpilot import db
from promptpilot.models import TaskCreate


def test_prompt_and_notes_survive_delivery_and_separate_attempts(isolated_db):
    from promptpilot import task_history as history

    task = db.create_task(TaskCreate(prompt="Исходное задание"))
    db.set_note(task.id, "Первое уточнение")
    db.set_note(task.id, "Второе уточнение")
    first = history.begin_attempt(task.id, "structured", "project")
    history.deliver_user_messages(first, "Второе уточнение")
    second = history.begin_attempt(task.id, "structured", "project")
    history.deliver_user_messages(second, "Второе уточнение")
    assert first != second
    messages = history.list_messages(task.id)
    assert [m["text"] for m in messages] == [
        "Исходное задание", "Первое уточнение", "Второе уточнение"]
    assert messages[0]["delivered_to"] == [first, second]
    assert messages[1]["delivered_to"] == []
    assert messages[2]["delivered_to"] == [first, second]
    db.clear_note(task.id)
    assert len(history.list_messages(task.id)) == 3


def test_message_revisions_and_source_deduplication(isolated_db):
    from promptpilot import task_history as history

    task = db.create_task(TaskCreate(prompt="Задание"))
    attempt = history.begin_attempt(task.id, "structured", None)
    history.append_message(task.id, attempt, "assistant", "Начало", "msg-1", partial=True)
    history.append_message(task.id, attempt, "assistant", "Начало", "msg-1", partial=True)
    history.append_message(task.id, attempt, "assistant", "Начало и конец", "msg-1")
    messages = history.list_messages(task.id)
    assert len(messages) == 2
    assert messages[1]["text"] == "Начало и конец"
    assert messages[1]["partial"] is False
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM task_messages WHERE role='assistant'").fetchone()[0] == 2


def test_gap_and_attempt_end_are_durable_and_deletion_cascades(isolated_db):
    from promptpilot import task_history as history

    task = db.create_task(TaskCreate(prompt="Задание"))
    attempt = history.begin_attempt(task.id, "detached", None)
    history.mark_gap(attempt, "detached_no_capture")
    history.mark_gap(attempt, "detached_no_capture")
    history.finish_attempt(attempt)
    attempts = history.list_attempts(task.id)
    assert attempts[0]["gaps"] == ["detached_no_capture"]
    assert attempts[0]["completed_at"] is not None
    assert db.delete_task(task.id)
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM task_messages").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task_history_attempts").fetchone()[0] == 0


def test_attempt_cannot_write_messages_into_another_task(isolated_db):
    import pytest
    from promptpilot import task_history as history

    first = db.create_task(TaskCreate(prompt="first"))
    other = db.create_task(TaskCreate(prompt="other"))
    attempt = history.begin_attempt(first.id, "structured", None)
    with pytest.raises(ValueError):
        history.append_message(other.id, attempt, "assistant", "wrong owner", "msg")
    with pytest.raises(ValueError):
        history.append_message(first.id, attempt, "tool", "command output", "msg")
