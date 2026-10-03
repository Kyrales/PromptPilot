"""Standalone, stdlib-only session reader, also sent over SSH for remote capture.

Only structured user/assistant text inside a unique PromptPilot boundary is returned.
It never returns an unclassified terminal transcript or the initial service prompt.
"""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_CANDIDATES = 100
BOUNDARY_RE = re.compile(r'<promptpilot-task-context\s+boundary="([^"\s]+)"')


def text_content(content, allowed=("text", "input_text", "output_text")):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content
                     if isinstance(block, dict) and block.get("type") in allowed
                     and isinstance(block.get("text"), str))


def conversation_record(event: dict):
    if event.get("type") in ("user", "assistant"):
        if event.get("isMeta"):
            return None
        message = event.get("message") or {}
        return event["type"], text_content(message.get("content")), event.get("uuid") or message.get("id")
    if event.get("type") == "response_item":
        message = event.get("payload") or {}
        if message.get("type") == "message" and message.get("role") in ("user", "assistant"):
            return message["role"], text_content(message.get("content")), message.get("id")
    return None


def _timestamp(event):
    value = event.get("timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def default_roots():
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return [str(claude / "projects"), str(codex / "sessions")]


def read_sessions(marker: str, since: float, roots=None, stop_at=None) -> dict:
    candidates, discovery_gaps = [], []
    for root in roots if roots is not None else default_roots():
        try:
            for path in Path(root).rglob("*.jsonl"):
                stat = path.stat()
                if stat.st_mtime >= since - 30:
                    candidates.append((stat.st_mtime, path))
        except OSError:
            discovery_gaps.append("session_directory_unreadable")
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if len(candidates) > MAX_CANDIDATES:
        discovery_gaps.append("session_candidate_limit")
    matches = []
    for _, path in candidates[:MAX_CANDIDATES]:
        gaps, messages, active, matched = [], [], False, False
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                start = max(0, size - MAX_FILE_BYTES)
                stream.seek(start)
                if start:
                    stream.readline()  # Omit a potentially cut first JSON record.
                    gaps.append("session_prefix_omitted")
                byte_offset = stream.tell()
                lines = stream.read(MAX_FILE_BYTES).splitlines(keepends=True)
        except OSError:
            discovery_gaps.append("session_file_unreadable")
            continue
        path_key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        for raw_line in lines:
            record_offset = byte_offset
            byte_offset += len(raw_line)
            line = raw_line.decode("utf-8", errors="replace")
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError()
            except ValueError:
                if active:
                    gaps.append("malformed_session_record" if line.endswith("\n") else "partial_session_record")
                continue
            timestamp = _timestamp(event)
            if active and stop_at is not None and timestamp is not None and timestamp > stop_at:
                break
            record = conversation_record(event)
            if not record:
                continue
            role, text, source_id = record
            if not text:
                continue
            boundary = BOUNDARY_RE.search(text) if role == "user" else None
            if boundary:
                if boundary[1] == marker:
                    active, matched = True, True
                    continue  # Initial prompt is already recorded, without service instructions.
                if active:
                    break  # Never traverse into another task in the same provider session.
            if active:
                messages.append({"role": role, "text": text,
                                 "source_key": f"session:{path_key}:{source_id or record_offset}",
                                 "partial": False})
        if matched:
            matches.append({"matched": True, "messages": messages,
                            "gaps": list(dict.fromkeys([*discovery_gaps, *gaps]))})
    if len(matches) > 1:
        return {"matched": False, "messages": [], "gaps": ["ambiguous_session_sources"]}
    return matches[0] if matches else {"matched": False, "messages": [],
                                      "gaps": list(dict.fromkeys([*discovery_gaps, "session_source_unavailable"]))}


if __name__ == "__main__":
    arguments = json.loads(sys.argv[1])
    print(json.dumps(read_sessions(**arguments), ensure_ascii=False))
