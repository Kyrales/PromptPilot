"""Durable conversation messages; terminal output is deliberately not a source."""

import json
import uuid

from . import db


SCHEMA = """
CREATE TABLE IF NOT EXISTS task_history_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    source TEXT NOT NULL,
    project_key TEXT,
    gaps TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_id INTEGER REFERENCES task_history_attempts(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS task_messages_source
    ON task_messages(task_id, attempt_id, source_key, id);
CREATE TABLE IF NOT EXISTS task_message_deliveries (
    message_id INTEGER NOT NULL REFERENCES task_messages(id) ON DELETE CASCADE,
    attempt_id INTEGER NOT NULL REFERENCES task_history_attempts(id) ON DELETE CASCADE,
    PRIMARY KEY(message_id, attempt_id)
);
"""


def record_user(conn, task_id: int, text: str, source_key: str):
    if text:
        conn.execute(
            "INSERT INTO task_messages(task_id, source_key, role, text, created_at) "
            "VALUES (?, ?, 'user', ?, ?)", (task_id, source_key, text, db._now()))


def begin_attempt(task_id: int, source: str, project_key: str | None) -> int:
    with db._connect() as conn:
        task = db.get_task(task_id, conn=conn)
        if not task:
            raise ValueError("Task not found")
        # A pre-upgrade queued task has no initial message yet.
        if not conn.execute("SELECT 1 FROM task_messages WHERE task_id=? AND source_key='prompt'",
                            (task_id,)).fetchone():
            record_user(conn, task_id, task.prompt, "prompt")
        cur = conn.execute(
            "INSERT INTO task_history_attempts(task_id, started_at, source, project_key) "
            "VALUES (?, ?, ?, ?)", (task_id, db._now(), source, project_key))
        return cur.lastrowid


def deliver_user_messages(attempt_id: int, note: str | None):
    with db._connect() as conn:
        attempt = conn.execute("SELECT task_id FROM task_history_attempts WHERE id=?",
                               (attempt_id,)).fetchone()
        if not attempt:
            raise ValueError("Attempt not found")
        rows = conn.execute(
            "SELECT id, source_key, text FROM task_messages WHERE task_id=? "
            "AND attempt_id IS NULL ORDER BY id", (attempt["task_id"],)).fetchall()
        prompt = next((r for r in rows if r["source_key"] == "prompt"), None)
        selected = [prompt] if prompt else []
        if note:
            found = next((r for r in reversed(rows)
                          if r["source_key"].startswith("note:") and r["text"] == note), None)
            if found is None:
                record_user(conn, attempt["task_id"], note, "note:" + uuid.uuid4().hex)
                found = {"id": conn.execute("SELECT last_insert_rowid()").fetchone()[0]}
            selected.append(found)
        conn.executemany(
            "INSERT OR IGNORE INTO task_message_deliveries VALUES (?, ?)",
            [(r["id"], attempt_id) for r in selected])


def append_message(task_id: int, attempt_id: int, role: str, text: str,
                   source_key: str, partial: bool = False):
    if role not in ("user", "assistant") or not isinstance(text, str) or not source_key:
        raise ValueError("Conversation messages require an explicit user/assistant role")
    with db._connect() as conn:
        owner = conn.execute("SELECT task_id FROM task_history_attempts WHERE id=?",
                             (attempt_id,)).fetchone()
        if not owner or owner[0] != task_id:
            raise ValueError("Attempt belongs to another task")
        last = conn.execute(
            "SELECT text, partial, role FROM task_messages WHERE task_id=? AND attempt_id=? "
            "AND source_key=? ORDER BY id DESC LIMIT 1", (task_id, attempt_id, source_key)).fetchone()
        if last and tuple(last) == (text, int(partial), role):
            return
        conn.execute(
            "INSERT INTO task_messages(task_id, attempt_id, source_key, role, text, created_at, partial) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, attempt_id, source_key, role, text, db._now(), int(partial)))


def mark_gap(attempt_id: int, reason: str):
    with db._connect(immediate=True) as conn:
        row = conn.execute("SELECT gaps FROM task_history_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row:
            gaps = json.loads(row[0])
            if reason not in gaps:
                conn.execute("UPDATE task_history_attempts SET gaps=? WHERE id=?",
                             (json.dumps([*gaps, reason]), attempt_id))


def clear_gap(attempt_id: int, reason: str):
    with db._connect(immediate=True) as conn:
        row = conn.execute("SELECT gaps FROM task_history_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row:
            conn.execute("UPDATE task_history_attempts SET gaps=? WHERE id=?",
                         (json.dumps([g for g in json.loads(row[0]) if g != reason]), attempt_id))


def finish_attempt(attempt_id: int):
    with db._connect() as conn:
        conn.execute("UPDATE task_history_attempts SET completed_at=COALESCE(completed_at, ?) WHERE id=?",
                     (db._now(), attempt_id))


def list_attempts(task_id: int, *, conn=None) -> list[dict]:
    if conn is None:
        with db._connect() as opened:
            return list_attempts(task_id, conn=opened)
    rows = conn.execute("SELECT * FROM task_history_attempts WHERE task_id=? ORDER BY id",
                        (task_id,)).fetchall()
    return [{**dict(r), "gaps": json.loads(r["gaps"])} for r in rows]


def list_messages(task_id: int, *, conn=None) -> list[dict]:
    if conn is None:
        with db._connect() as opened:
            return list_messages(task_id, conn=opened)
    rows = conn.execute(
        "SELECT m.* FROM task_messages m WHERE m.task_id=? AND m.id IN "
        "(SELECT MAX(id) FROM task_messages WHERE task_id=? GROUP BY attempt_id, source_key) "
        "ORDER BY COALESCE((SELECT MIN(id) FROM task_messages first "
        "WHERE first.task_id=m.task_id AND first.attempt_id IS m.attempt_id "
        "AND first.source_key=m.source_key), m.id)", (task_id, task_id)).fetchall()
    deliveries = conn.execute(
        "SELECT d.* FROM task_message_deliveries d JOIN task_messages m ON m.id=d.message_id "
        "WHERE m.task_id=? ORDER BY d.attempt_id", (task_id,)).fetchall()
    return [{**dict(r), "partial": bool(r["partial"]),
             "delivered_to": [d["attempt_id"] for d in deliveries if d["message_id"] == r["id"]]}
            for r in rows]
