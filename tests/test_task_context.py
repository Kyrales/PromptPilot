import json
import asyncio

import pytest
from click.testing import CliRunner
import httpx

from promptpilot import db, task_history as history
from promptpilot.models import TaskCreate


def tasks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    current = db.create_task(TaskCreate(prompt="current", working_dir=str(project)))
    target = db.create_task(TaskCreate(prompt="target", working_dir=str(project)))
    return current, target


def test_snapshot_freezes_updates_and_reconstructs_long_messages(isolated_db, tmp_path):
    from promptpilot import task_context as context
    current, target = tasks(tmp_path)
    attempt = history.begin_attempt(target.id, "structured", None)
    original = "Ответ" * 7000
    history.append_message(target.id, attempt, "assistant", original, "message", partial=True)
    first = context.read_task(target.id, current.id, limit=1000)
    history.append_message(target.id, attempt, "assistant", "НОВАЯ ВЕРСИЯ", "message")
    history.append_message(target.id, attempt, "assistant", "позднее сообщение", "second")
    pages = [first]
    while pages[-1]["next_cursor"]:
        pages.append(context.read_task(target.id, current.id, pages[-1]["next_cursor"], limit=1000))
    texts = [m["text"] for page in pages for m in page["messages"] if m["role"] == "assistant"]
    assert "".join(texts) == original
    assert all(sum(len(m["text"]) for m in p["messages"]) <= 1000 for p in pages)
    assert all(m["partial"] for p in pages for m in p["messages"] if m["role"] == "assistant")
    new = context.read_task(target.id, current.id)
    assert [m["text"] for m in new["messages"]][-2:] == ["НОВАЯ ВЕРСИЯ", "позднее сообщение"]


def test_deleted_task_expiry_and_cursor_ownership(isolated_db, tmp_path):
    from promptpilot import task_context as context
    current, target = tasks(tmp_path)
    page = context.read_task(target.id, current.id, limit=1)
    stranger = db.create_task(TaskCreate(prompt="stranger"))
    with pytest.raises(ValueError):
        context.read_task(target.id, stranger.id, page["next_cursor"])
    with db._connect() as conn:
        conn.execute("UPDATE task_context_snapshots SET expires_at=0")
    with pytest.raises(ValueError, match="expired"):
        context.read_task(target.id, current.id, page["next_cursor"])
    assert db.delete_task(target.id)
    with pytest.raises(LookupError):
        context.read_task(target.id, current.id, page["next_cursor"])
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM task_context_snapshots").fetchone()[0] == 0


def test_legacy_result_and_continuations_are_not_conversation(isolated_db, tmp_path):
    from promptpilot import task_context as context
    current, target = tasks(tmp_path)
    db.create_task(TaskCreate(prompt="secret continuation", parent_task_id=target.id))
    with db._connect() as conn:
        conn.execute("DELETE FROM task_messages WHERE task_id=?", (target.id,))
        conn.execute("UPDATE tasks SET result=? WHERE id=?", ("TOOL OUTPUT and terminal", target.id))
    page = context.read_task(target.id, current.id)
    assert [m["text"] for m in page["messages"]] == ["target"]
    assert page["coverage"]["complete"] is False
    assert "legacy_unavailable" in page["coverage"]["gaps"]


def test_search_scopes_projects_and_returns_matching_snippet(isolated_db, tmp_path):
    from promptpilot import task_context as context
    current, target = tasks(tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = db.create_task(TaskCreate(prompt="Ошибка сокета", working_dir=str(other_dir)))
    remote = db.create_task(TaskCreate(prompt="Ошибка сокета", working_dir=target.working_dir, machine="remote"))
    attempt = history.begin_attempt(target.id, "structured", context.project_identity(target.working_dir))
    history.append_message(target.id, attempt, "assistant", "x" * 1000 + "ОШИБКА сокета найдена", "reply")
    result = context.search_tasks(current.id, "ошибка сокета")
    assert [t["task_id"] for t in result["tasks"]] == [target.id]
    assert "ОШИБКА сокета" in result["tasks"][0]["snippet"]
    all_result = context.search_tasks(current.id, "ошибка сокета", all_projects=True)
    assert {t["task_id"] for t in all_result["tasks"]} == {target.id, other.id, remote.id}
    no_project = db.create_task(TaskCreate(prompt="no project"))
    with pytest.raises(ValueError, match="project"):
        context.search_tasks(no_project.id)
    assert context.read_task(other.id, current.id)["task"]["task_id"] == other.id


def test_readonly_context_token_cannot_authorize_queue_writes(isolated_db, tmp_path, monkeypatch):
    from promptpilot import api, task_context as context
    current, target = tasks(tmp_path)
    monkeypatch.setattr(api, "API_TOKEN", "operator-secret")
    token = context.issue_token(current.id)
    def request(method, path, **kwargs):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(run())
    headers = {"Authorization": "Bearer " + token}
    response = request("GET", f"/api/context/read/{target.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["task"]["task_id"] == target.id
    assert request("GET", "/api/tasks", headers=headers).status_code == 401
    assert request("POST", "/api/tasks", json={"prompt": "unauthorized"}, headers=headers).status_code == 401
    assert request("GET", f"/api/context/read/{target.id}", headers={"Authorization": "Bearer " + token + "bad"}).status_code == 401
    monkeypatch.setattr(api, "API_TOKEN", "")
    assert request("POST", "/api/tasks", json={"prompt": "unauthorized"}, headers=headers).status_code == 401


def test_cli_access_file_pins_source_and_never_falls_back(isolated_db, tmp_path, monkeypatch):
    from promptpilot import task_context as context
    from promptpilot.cli import cli
    current, target = tasks(tmp_path)
    access = tmp_path / "access.json"
    access.write_text(json.dumps({"task_id": current.id, "instance_id": context.instance_id(),
                                  "db_path": str(db.DB_PATH)}), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["context", "--access-file", str(access), "read", str(target.id)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["task"]["task_id"] == target.id
    access.write_text(json.dumps({"task_id": current.id, "instance_id": "wrong", "db_path": str(db.DB_PATH)}))
    result = runner.invoke(cli, ["context", "--access-file", str(access), "read", str(target.id)])
    assert result.exit_code != 0
    access.write_text(json.dumps({"task_id": current.id, "instance_id": context.instance_id(),
                                  "url": "http://127.0.0.1:1", "token": "unavailable"}))
    result = runner.invoke(cli, ["context", "--access-file", str(access), "read", str(target.id)])
    assert result.exit_code != 0
    assert '"task_id"' not in result.output
