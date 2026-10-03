import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from promptpilot import db, task_history as history, task_context as context, worker
from promptpilot.models import TaskCreate


@pytest.mark.parametrize("exit_code", [0, 1])
def test_real_headless_execution_saves_messages_and_injects_access(isolated_db, tmp_path, monkeypatch, exit_code):
    captured = tmp_path / "prompt.txt"
    script = tmp_path / "provider.py"
    script.write_text(
        "import json,pathlib,sys\n"
        "prompt=sys.stdin.read()\n"
        f"pathlib.Path({str(captured)!r}).write_text(prompt, encoding='utf-8')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'test-thread'}))\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'a1','type':'agent_message','text':'Saved answer'}}))\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'tool','type':'command_execution','aggregated_output':'SECRET TOOL'}}))\n",
        encoding="utf-8")
    with script.open("a", encoding="utf-8") as stream:
        stream.write(f"sys.exit({exit_code})\n")
    providers = {"test-provider": {"prompt_stdin": True}}
    monkeypatch.setattr(worker, "load_providers", lambda: providers)
    monkeypatch.setattr(worker, "build_cmd", lambda *_args, **_kwargs: [sys.executable, str(script)])
    monkeypatch.setattr(worker, "get_provider_env", lambda _provider: os.environ.copy())
    db.create_task(TaskCreate(prompt="Continue using task 21", provider="test-provider", working_dir=str(tmp_path)))
    task = db.get_next_runnable()
    worker.execute_task(task)
    assert db.get_task(task.id).status.value == ("completed" if exit_code == 0 else "failed")
    assert [m["text"] for m in history.list_messages(task.id)] == ["Continue using task 21", "Saved answer"]
    prompt = captured.read_text(encoding="utf-8")
    assert "<promptpilot-task-context" in prompt
    assert "--access-file" in prompt
    attempt = history.list_attempts(task.id)[0]
    assert attempt["completed_at"] is not None
    assert attempt["project_key"] == context.project_identity(str(tmp_path))


def test_access_instruction_for_target_does_not_depend_on_stale_environment(isolated_db, tmp_path, monkeypatch):
    task = db.create_task(TaskCreate(prompt="task", working_dir=str(tmp_path)))
    attempt = history.begin_attempt(task.id, "session", None)
    monkeypatch.setenv("PP_TASK_ID", "9999")
    instruction, access_file = context.prepare_access(task, attempt)
    assert "--access-file" in instruction
    assert f"задача №{task.id}" in instruction
    access = json.loads(Path(access_file).read_text(encoding="utf-8"))
    assert access["task_id"] == task.id
    assert access["db_path"] == str(db.DB_PATH.resolve())
    assert access["instance_id"] == context.instance_id()
    assert "token" not in instruction


def test_existing_worktrees_share_project_identity(isolated_db, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "init"], check=True, capture_output=True)
    checkout = tmp_path / "other-checkout"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(checkout)], check=True, capture_output=True)
    assert context.project_identity(str(root)) == context.project_identity(str(checkout))
    assert context.project_identity(str(root), machine="remote") is None


def test_remote_access_without_origin_url_is_explicitly_unavailable(isolated_db, tmp_path, monkeypatch):
    from promptpilot.remote import Remote
    task = db.create_task(TaskCreate(prompt="task", working_dir=str(tmp_path), machine="remote"))
    attempt = history.begin_attempt(task.id, "session", None)
    monkeypatch.delenv("PP_CONTEXT_URL", raising=False)
    instruction, access_file = context.prepare_access(task, attempt, host=Remote("example.invalid"))
    assert access_file == ""
    assert "недоступ" in instruction
    assert "localhost" not in instruction


def test_remote_access_copies_readonly_credential_not_admin_secret(isolated_db, tmp_path, monkeypatch):
    from promptpilot.remote import Remote
    task = db.create_task(TaskCreate(prompt="task", machine="remote"))
    attempt = history.begin_attempt(task.id, "session", None)
    monkeypatch.setenv("PP_CONTEXT_URL", "https://origin.example.invalid")
    monkeypatch.setenv("PP_API_TOKEN", "ADMIN MUST NEVER BE FORWARDED")
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="/home/user/.promptpilot/context/access.json\n", stderr="")
    monkeypatch.setattr(context.subprocess, "run", run)
    instruction, access_file = context.prepare_access(task, attempt, host=Remote("example.invalid"))
    assert access_file == "/home/user/.promptpilot/context/access.json"
    assert "--access-file" in instruction
    sent = calls[0][1]["input"]
    access = json.loads(sent)
    assert context.verify_token(access["token"]) == task.id
    assert access["url"] == "https://origin.example.invalid"
    assert "ADMIN MUST NEVER BE FORWARDED" not in sent
    assert access["token"] not in instruction


def test_two_tasks_in_existing_herdr_pane_have_separate_history_and_real_project(isolated_db, tmp_path, monkeypatch):
    from promptpilot import herdr_exec
    actual = tmp_path / "actual"
    actual.mkdir()
    claude_root = tmp_path / "claude"
    sessions = claude_root / "projects" / "actual"
    sessions.mkdir(parents=True)
    session = sessions / "session.jsonl"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda _host: None)
    monkeypatch.setattr(herdr_exec, "_stabilize_workflow_completion", lambda *_a, **_k: ("done", ""))
    submitted = []
    def run(args, host=None, timeout=None):
        if args[:2] == ["agent", "get"]:
            return 0, {"result": {"agent": {"agent_status": "idle", "pane_id": "same-pane", "cwd": str(actual)}}}, ""
        if args[:2] == ["agent", "prompt"]:
            submitted.append(args[3])
            with session.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "user", "message": {"content": args[3]}}) + "\n")
                stream.write(json.dumps({"type": "assistant", "uuid": "a" + str(len(submitted)),
                                         "message": {"content": [{"type": "text", "text": "answer " + str(len(submitted))}]}}) + "\n")
            return 0, {"result": {"agent": {"agent_status": "done"}}}, ""
        if args[:2] == ["agent", "read"]:
            return 0, None, "TERMINAL TOOL OUTPUT MUST NOT BE HISTORY"
        raise AssertionError(args)
    monkeypatch.setattr(herdr_exec, "_run", run)
    task_ids = []
    for number in (1, 2):
        task = db.create_task(TaskCreate(prompt=f"task {number}", working_dir=str(tmp_path / "wrong-cwd"),
                                        provider="claude-herdr", herdr_target="same-pane"))
        execution = history.Execution(task)
        token = history.active_execution.set(execution)
        try:
            execution.start("session", env={"CLAUDE_CONFIG_DIR": str(claude_root)})
            outcome = herdr_exec.run_in_herdr(task, {"kind": "claude"}, prompt_override=task.prompt)
            assert outcome["ok"] is True
        finally:
            execution.close()
            history.active_execution.reset(token)
        task_ids.append(task.id)
        assert history.list_attempts(task.id)[0]["project_key"] == context.project_identity(str(actual))
    assert [m["text"] for m in history.list_messages(task_ids[0])] == ["task 1", "answer 1"]
    assert [m["text"] for m in history.list_messages(task_ids[1])] == ["task 2", "answer 2"]
    assert submitted[0] != submitted[1]


def test_delivery_storage_failure_does_not_abort_provider_task(isolated_db, tmp_path, monkeypatch):
    task = db.create_task(TaskCreate(prompt="task", working_dir=str(tmp_path)))
    execution = history.Execution(task)
    execution.start("structured")
    def fail(*args):
        raise OSError("storage unavailable")
    monkeypatch.setattr(history, "deliver_user_messages", fail)
    execution.delivered()
    execution.close()
    assert "delivery_storage_error" in history.list_attempts(task.id)[0]["gaps"]


def test_custom_plain_output_is_not_promoted_to_assistant_history(isolated_db, tmp_path, monkeypatch):
    script = tmp_path / "plain.py"
    script.write_text("print('UNCLASSIFIED TERMINAL OUTPUT')\n", encoding="utf-8")
    monkeypatch.setattr(worker, "load_providers", lambda: {"plain": {}})
    monkeypatch.setattr(worker, "build_cmd", lambda *_a, **_k: [sys.executable, str(script)])
    monkeypatch.setattr(worker, "get_provider_env", lambda _provider: os.environ.copy())
    db.create_task(TaskCreate(prompt="task", provider="plain", working_dir=str(tmp_path)))
    task = db.get_next_runnable()
    worker.execute_task(task)
    assert db.get_task(task.id).status.value == "completed"
    page = context.read_task(task.id, task.id)
    assert [m["text"] for m in page["messages"]] == ["task"]
    assert page["coverage"]["complete"] is False
    assert "unclassified_output" in page["coverage"]["gaps"]


def test_detached_history_explicitly_reports_absent_capture(isolated_db, tmp_path):
    task = db.create_task(TaskCreate(prompt="task", working_dir=str(tmp_path), detached=True))
    execution = history.Execution(task)
    execution.start("structured")
    execution.close()
    page = context.read_task(task.id, task.id)
    assert [m["text"] for m in page["messages"]] == ["task"]
    assert "detached_no_capture" in page["coverage"]["gaps"]
