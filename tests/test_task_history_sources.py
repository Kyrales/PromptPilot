import io
import json

from promptpilot import db, task_history as history
from promptpilot.models import TaskCreate


def test_stream_filters_tools_reasoning_errors_and_deduplicates(isolated_db):
    task = db.create_task(TaskCreate(prompt="user task"))
    attempt = history.begin_attempt(task.id, "structured", None)
    collector = history.StreamCollector(task.id, attempt)
    events = [
        {"type": "assistant", "message": {"id": "claude-1", "content": [
            {"type": "text", "text": "Claude answer"},
            {"type": "tool_use", "input": {"command": "SECRET TOOL"}},
            {"type": "thinking", "thinking": "SECRET REASONING"}]}},
        {"type": "item.completed", "item": {"id": "codex-1", "type": "agent_message", "text": "Codex answer"}},
        {"type": "item.completed", "item": {"id": "command", "type": "command_execution", "aggregated_output": "SECRET TOOL"}},
        {"type": "error", "message": "not conversation"},
    ]
    for event in events + events:
        collector.feed(json.dumps(event))
    assert [m["text"] for m in history.list_messages(task.id)] == ["user task", "Claude answer", "Codex answer"]


def test_pipe_records_before_process_completion_and_keeps_draining_on_storage_error(isolated_db, monkeypatch):
    from promptpilot import worker
    task = db.create_task(TaskCreate(prompt="task"))
    attempt = history.begin_attempt(task.id, "structured", None)
    collector = history.StreamCollector(task.id, attempt)
    lines = [json.dumps({"type": "item.completed", "item": {
        "id": "m1", "type": "agent_message", "text": "answer"}}) + "\n", "unclassified output\n"]
    chunks = []
    worker._read_process_pipe(io.StringIO("".join(lines)), chunks, task.id, collector)
    assert history.list_messages(task.id)[-1]["text"] == "answer"
    assert len(chunks) == 2
    def fail(*args, **kwargs):
        raise OSError("storage unavailable")
    monkeypatch.setattr(history, "append_message", fail)
    chunks = []
    worker._read_process_pipe(io.StringIO("".join(lines)), chunks, task.id, collector)
    assert len(chunks) == 2
    assert "storage_error" in history.list_attempts(task.id)[0]["gaps"]


def test_session_reader_reads_only_attributed_user_assistant_text(tmp_path):
    from promptpilot.task_history_source import read_sessions
    marker = '<promptpilot-task-context boundary="own-unique-marker">'
    records = [
        {"type": "user", "message": {"content": "earlier conversation"}},
        {"type": "user", "uuid": "initial", "message": {"content": "task\n" + marker}},
        {"type": "assistant", "uuid": "a1", "message": {"content": [
            {"type": "text", "text": "Found the cause"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "SECRET"}}]}},
        {"type": "user", "uuid": "tool", "message": {"content": [{"type": "tool_result", "content": "SECRET"}]}},
        {"type": "user", "uuid": "u2", "message": {"content": "Please explain"}},
        {"type": "assistant", "uuid": "a2", "message": {"content": [{"type": "text", "text": "Explanation"}]}},
        {"type": "user", "message": {"content": '<promptpilot-task-context boundary="later-marker">'}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "CONTINUATION MUST NOT LEAK"}]}},
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    result = read_sessions("own-unique-marker", 0, roots=[str(tmp_path)])
    assert [(m["role"], m["text"]) for m in result["messages"]] == [
        ("assistant", "Found the cause"), ("user", "Please explain"), ("assistant", "Explanation")]
    assert result["matched"] is True
    assert result["gaps"] == []


def test_codex_session_reader_excludes_developer_reasoning_and_tool_items(tmp_path):
    from promptpilot.task_history_source import read_sessions
    records = [
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": '<promptpilot-task-context boundary="codex-marker">'}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "SECRET REASONING"}]}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "SECRET TOOL"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "SECRET INSTRUCTIONS"}]}},
        {"type": "response_item", "payload": {"id": "a", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Visible answer"}]}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Visible answer"}},
    ]
    (tmp_path / "session.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    result = read_sessions("codex-marker", 0, roots=[str(tmp_path)])
    assert [m["text"] for m in result["messages"]] == ["Visible answer"]


def test_session_malformed_and_missing_source_are_explicit_gaps(tmp_path):
    from promptpilot.task_history_source import read_sessions
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": '<promptpilot-task-context boundary="marker">'}})
                    + "\n{malformed}\n{unfinished", encoding="utf-8")
    result = read_sessions("marker", 0, roots=[str(tmp_path)])
    assert "malformed_session_record" in result["gaps"]
    assert "partial_session_record" in result["gaps"]
    assert read_sessions("not-present", 0, roots=[str(tmp_path)])["matched"] is False


def test_remote_session_reader_runs_standalone_through_transport(isolated_db, tmp_path, monkeypatch):
    import sys
    from promptpilot import remote
    root = tmp_path / "claude"
    sessions = root / "projects" / "project"
    sessions.mkdir(parents=True)
    records = [
        {"type": "user", "message": {"content": '<promptpilot-task-context boundary="remote-marker">'}},
        {"type": "assistant", "uuid": "message", "message": {"content": [{"type": "text", "text": "Remote answer"}]}},
    ]
    (sessions / "session.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    # Replace only the external SSH transport with a local subprocess; execute the real reader script.
    monkeypatch.setattr(remote, "ssh_command", lambda host, argv, env=None: [sys.executable, *argv[1:]])
    task = db.create_task(TaskCreate(prompt="task", machine="remote"))
    attempt = history.begin_attempt(task.id, "session", None)
    collector = history.SessionCollector(task.id, attempt, "remote-marker", host=remote.Remote("test.invalid"))
    collector.capture()
    assert [m["text"] for m in history.list_messages(task.id)] == ["task", "Remote answer"]
