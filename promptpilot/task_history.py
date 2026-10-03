"""Durable conversation messages; terminal output is deliberately not a source."""

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from contextvars import ContextVar

from . import db


SCHEMA = """
CREATE TABLE IF NOT EXISTS task_history_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    source TEXT NOT NULL,
    boundary TEXT NOT NULL DEFAULT '',
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


def initialize(conn):
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(task_history_attempts)")}
    if "boundary" not in columns:
        conn.execute("ALTER TABLE task_history_attempts ADD COLUMN boundary TEXT NOT NULL DEFAULT ''")


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
            "INSERT INTO task_history_attempts(task_id, started_at, source, project_key, boundary) "
            "VALUES (?, ?, ?, ?, ?)", (task_id, db._now(), source, project_key, uuid.uuid4().hex))
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


class StreamCollector:
    def __init__(self, task_id: int, attempt_id: int):
        self.task_id, self.attempt_id = task_id, attempt_id
        self.parts = {}
        self.gaps = set()
        self.has_message = False

    def gap(self, reason):
        self.gaps.add(reason)
        try:
            mark_gap(self.attempt_id, reason)
        except Exception:
            pass  # Draining the provider pipe must survive unavailable storage.

    def feed(self, line: str):
        from .task_history_source import text_content
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                return
            kind = event.get("type")
            text, key = "", None
            if kind == "assistant":
                message = event.get("message") or {}
                text = text_content(message.get("content"), ("text",))
                key = message.get("id") or event.get("uuid")
                if text and key:
                    parts = self.parts.setdefault(key, [])
                    if text not in parts:
                        parts.append(text)
                    text = "\n".join(parts)
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    text, key = item.get("text"), item.get("id")
            elif kind == "text":
                part = event.get("part") or {}
                text, key = part.get("text"), part.get("id")
            elif kind == "result" and not event.get("is_error") and not self.has_message:
                text, key = event.get("result"), "result"
            if isinstance(text, str) and text:
                key = str(key or hashlib.sha256(text.encode()).hexdigest())
                append_message(self.task_id, self.attempt_id, "assistant", text, "stream:" + key)
                self.has_message = True
                clear_gap(self.attempt_id, "source_not_verified")
        except json.JSONDecodeError:
            if line.strip():
                self.gap("unclassified_output")
        except Exception:
            self.gap("storage_error")

    def finish(self):
        for reason in self.gaps:
            self.gap(reason)


class SessionCollector:
    def __init__(self, task_id: int, attempt_id: int, marker: str, host=None, env=None):
        self.task_id, self.attempt_id, self.marker = task_id, attempt_id, marker
        self.host, self.env = host, env or {}
        self.since = time.time()
        self.stop_event = threading.Event()
        self.thread = None

    def capture(self, stop_at=None):
        from . import task_history_source as source
        try:
            roots = None
            if self.host:
                from .remote import ssh_command
                script = Path(source.__file__).with_suffix(".py").read_text(encoding="utf-8")
                args = json.dumps({"marker": self.marker, "since": self.since, "stop_at": stop_at})
                env = {k: v for k, v in self.env.items() if k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
                result = subprocess.run(ssh_command(self.host, ["python", "-c", "import sys;exec(sys.stdin.read())", args], env),
                                        input=script, capture_output=True, text=True, encoding="utf-8", timeout=15)
                if result.returncode:
                    raise OSError("remote session reader unavailable")
                captured = json.loads(result.stdout)
            else:
                if self.env.get("CLAUDE_CONFIG_DIR") or self.env.get("CODEX_HOME"):
                    roots = [str(Path(self.env.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"),
                             str(Path(self.env.get("CODEX_HOME", Path.home() / ".codex")) / "sessions")]
                captured = source.read_sessions(self.marker, self.since, roots=roots, stop_at=stop_at)
            for message in captured["messages"]:
                append_message(self.task_id, self.attempt_id, **message)
            for reason in captured["gaps"]:
                mark_gap(self.attempt_id, reason)
            if captured["matched"]:
                clear_gap(self.attempt_id, "source_not_verified")
                clear_gap(self.attempt_id, "session_source_unavailable")
        except Exception:
            try:
                mark_gap(self.attempt_id, "session_capture_unavailable")
            except Exception:
                print(f"task history #{self.task_id}: storage unavailable", file=sys.stderr)

    def start(self):
        def collect():
            while not self.stop_event.wait(3):
                self.capture()
        self.thread = threading.Thread(target=collect, daemon=True)
        self.thread.start()

    def stop(self):
        stopped_at = time.time()
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)
        self.capture(stop_at=stopped_at)


active_execution = ContextVar("promptpilot_history_execution", default=None)


class Execution:
    """One execution lifetime; no attempt is allocated for preflight-only tasks."""

    def __init__(self, task):
        self.task = task
        self.attempt_id = None
        self.stream = None
        self.session = None
        self.marker = None

    def start(self, source, host=None, env=None):
        from . import task_context
        self.attempt_id = begin_attempt(
            self.task.id, source, task_context.project_identity(
                self.task.working_dir, getattr(self.task, "machine", None), host))
        self.marker = next(a["boundary"] for a in list_attempts(self.task.id) if a["id"] == self.attempt_id)
        mark_gap(self.attempt_id, "source_not_verified")
        if getattr(self.task, "detached", False):
            mark_gap(self.attempt_id, "detached_no_capture")
        elif source == "structured":
            self.stream = StreamCollector(self.task.id, self.attempt_id)
        else:
            self.session = SessionCollector(self.task.id, self.attempt_id, self.marker, host, env)
            self.session.start()

    def set_project(self, cwd, host=None):
        from . import task_context
        key = task_context.project_identity(cwd, getattr(self.task, "machine", None), host)
        try:
            with db._connect() as conn:
                conn.execute("UPDATE task_history_attempts SET project_key=? WHERE id=?", (key, self.attempt_id))
        except Exception:
            self._gap("project_identity_unavailable")

    def inject(self, prompt, host=None):
        from .task_context import prepare_access
        from .herdr_exec import WORKFLOW_CONTRACT_MARKER, WORKFLOW_CONTRACT_END
        suffix = ""
        if prompt.rstrip().endswith(WORKFLOW_CONTRACT_END) and WORKFLOW_CONTRACT_MARKER in prompt:
            position = prompt.rfind(WORKFLOW_CONTRACT_MARKER)
            prompt, suffix = prompt[:position].rstrip(), prompt[position:]
        try:
            instruction, _ = prepare_access(self.task, self.attempt_id, host)
        except Exception:
            instruction = "Чтение контекста других задач недоступно: не удалось подготовить доступ."
        return (prompt.rstrip() + f'\n\n<promptpilot-task-context boundary="{self.marker}">\n'
                + instruction + "\n</promptpilot-task-context>" + ("\n\n" + suffix if suffix else ""))

    def delivered(self):
        if self.attempt_id:
            try:
                deliver_user_messages(self.attempt_id, getattr(self.task, "note", None))
            except Exception:
                self._gap("delivery_storage_error")

    def _gap(self, reason):
        try:
            mark_gap(self.attempt_id, reason)
        except Exception:
            print(f"task history #{self.task.id}: {reason}", file=sys.stderr)

    def close(self):
        if self.attempt_id is None:
            return
        try:
            if self.stream:
                self.stream.finish()
            if self.session:
                self.session.stop()
            finish_attempt(self.attempt_id)
        except Exception:
            print(f"task history #{self.task.id}: final capture unavailable", file=sys.stderr)
