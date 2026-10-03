# Task history context implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Agents inside PromptPilot read another task's conversation and search related tasks only when the user requests it.

**Architecture:** SQLite stores immutable message revisions, independent execution attempts and delivery records. A shared context service materializes one-hour snapshots and exposes bounded read/search through CLI and read-only API routes. Structured streams and session-file adapters collect conversation messages; unsupported sources report incomplete coverage.

**Tech Stack:** Existing Python, SQLite, Click, FastAPI; standard library for transport and signing, no new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-10-03-task-history-context-design.md`.

## Global constraints

- Keep current result rendering, notifications and queue behavior.
- User and assistant text only; never promote terminal output, tool output or reasoning to conversation messages.
- No automatic cross-task reads, no waits, no writes to other tasks, no automatic continuation traversal.
- Default page budget: 12000 text characters; search page: at most 10 cards; snapshot TTL: 1 hour.
- All execution modes: collect trustworthy messages where available, explicitly report gaps elsewhere.
- Source instance is authoritative; context credentials cannot authorize other API routes.
- Use the existing requested branch; user has authorized two sol high reviews followed by implementation and final sol review, without further handoff questions.

## Review focus

- Existing herdr target has stale environment and may have a different cwd: explicit access-file invocation and resolved pane cwd.
- Retry without incrementing retry_count: separate attempt IDs and durable user delivery records.
- Updated assistant answer between pages: materialize snapshots, never concatenate differing versions.
- Task deleted after first page: invalidate snapshot via foreign-key cascade and task existence check.
- Custom/plain terminal provider: retain only proven roles and flag unsupported capture; never treat stdout as an assistant by default.

### Task 1: Durable message history

**Files:** create `promptpilot/task_history.py`, `tests/test_task_history.py`; modify `promptpilot/db.py`.

**Interfaces:** `record_user(conn, task_id, text, source_key)`, `begin_attempt(task_id, source, project_key) -> int`, `append_message(task_id, attempt_id, role, text, source_key, partial=False)`, `deliver_user_messages(attempt_id, note)`, `mark_gap(attempt_id, reason)`, `finish_attempt(attempt_id)`.

- [x] Write tests for initial prompt, note edits and delivery, separate retries, source deduplication/revisions, deleted-task cleanup and unsupported-source coverage. Assert exact roles/text and durable records after reopening the database.
- [x] Run `./.venv/Scripts/python.exe -m pytest tests/test_task_history.py -q`; confirm feature assertions fail.
- [x] Add history tables with cascading task foreign keys to DB schema. Record prompts/notes within existing transactions. Persist immutable message revisions and attempt rows; do not use retry_count as attempt identity.
- [x] Run the history tests and `tests/test_db_init.py`, `tests/test_schedule_series.py`.
- [x] Commit storage and tests.

### Task 2: Snapshot read, scoped search and credentials

**Files:** create `promptpilot/task_context.py`, `tests/test_task_context.py`; modify `db.py`, `api.py`, `cli.py`.

**Interfaces:** `read_task(task_id, current_task_id, cursor=None, limit=12000) -> dict`, `search_tasks(current_task_id, query='', all_projects=False, offset=0) -> dict`, `issue_token(current_task_id) -> str`, `verify_token(token) -> int | None`.

- [x] Write tests reconstructing a long message across pages and freezing revisions/new messages, expired/invalid/cross-task cursors, missing/deleted task, no continuation traversal, legacy prompt-only fallback, project/machine boundaries and explicit all-project search. Assert search snippets match query and contain no unrelated full dialogue.
- [x] Write API/CLI tests for existing auth and a context token that permits only GET context routes; malformed/expired tokens cannot bypass auth, including when general API auth is disabled. CLI local access is bound to explicit DB path and instance ID; remote failures do not fall back to local data.
- [x] Confirm failures with `pytest tests/test_task_context.py -q`.
- [x] Implement materialized snapshot tables (JSON message copies and cursor position, TTL cleanup), bounded pagination, basic Unicode casefold substring search and brief cards. Legacy records supply prompt and gap notice, not unclassified result text.
- [x] Add `pp context --access-file PATH read ID [--cursor CURSOR] [--limit N]` and `pp context --access-file PATH search [QUERY] [--all-projects] [--offset N]`; JSON output. Add `/api/context/read/{task_id}` and `/api/context/search`; signed current-task credentials accepted exclusively on these GET routes.
- [x] Run focused tests and existing API/CLI/auth coverage; commit.

### Task 3: Stream and session adapters

**Files:** create `promptpilot/task_history_source.py`, `tests/test_task_history_sources.py`; modify `task_history.py`, `worker.py`, `herdr_exec.py`.

**Interfaces:** `StreamCollector(task_id, attempt_id).feed(line)`; stdlib session-source reader accepts marker, earliest time and provider roots, returns only attributed user/assistant messages and coverage information. `SessionCollector` owns polling, final capture and stop.

- [x] Write realistic Claude/Codex stream and JSONL session fixtures with text, tools, reasoning, duplicate IDs, malformed/truncated records and two task markers in one session. Assert exact extracted conversation and no later-task messages.
- [x] Test that running streams persist before task completion, failed/cancelled attempts preserve messages, storage errors do not stop draining pipes, session close retains captured messages, unsupported/detached sources are incomplete.
- [x] Run failing source tests.
- [x] Implement allowlisted structured-message extraction. Use unique attempt markers for session-file attribution, not terminal-screen role guessing. Local and SSH session capture return bounded structured messages; unsupported adapter/permission/file failures yield explicit gaps. Keep raw terminal output out of history.
- [x] Integrate collectors around actual provider execution, with final capture and guaranteed stop; track separate immutable attempt IDs. Use recorded input deliveries to avoid storing synthetic service prompts as user messages.
- [x] Run focused tests, `tests/test_codex_prompt_transport.py`, `tests/test_herdr_workflow_completion.py`, `tests/test_process_tree.py`; commit.

### Task 4: Agent access and project identity for every launch

**Files:** modify `task_context.py`, `worker.py`, `herdr_exec.py`, `.env.example`; create `tests/test_task_context_execution.py`.

**Interfaces:** `prepare_access(task, attempt_id, host=None) -> (instruction: str, access_file: str)`; `project_identity(path, machine=None, host=None) -> str | None`.

- [x] Write tests for normal/headless/herdr/target/remote invocation: explicit source instance and current task, same-repository existing worktree identity, remote machine separation, no secret in saved messages, stale target env ignored, no scope widening without user request instruction.
- [x] Confirm failures.
- [x] Resolve Git common directory on execution machine, normalize non-Git absolute paths, persist attempt identity; get actual cwd for attached target panes.
- [x] Generate private task-scoped access files. Local file pins absolute DB path and instance ID; remote file contains configured `PP_CONTEXT_URL`, read-only token and expected instance ID. Copy remote file through existing SSH machinery without logging secrets. No remote URL means explicit unavailable instruction, no fallback.
- [x] Inject a short instruction after provider routing and before every actual launch, including pre-existing targets; use explicit `--access-file` so stale environment cannot select another task. Use current executable/module invocation locally and installed `pp` remotely. Keep service instructions and credentials out of user-message storage. Preserve closing verdict contract ordering.
- [x] Run execution tests and existing headless/herdr/pipeline transport checks; commit.

### Task 5: Documentation, final review and verification

**Files:** update `README.md`, review record and this plan.

- [ ] Document natural-language scenarios, CLI and API, remote URL, partial history, legacy limits, snapshot expiry and deletion behavior; keep protocol details out of product UI.
- [ ] Run `./.venv/Scripts/python.exe -m pytest -q` and `./.venv/Scripts/python.exe -m ruff check promptpilot tests tools main.py`; check `git diff --check`.
- [ ] Request independent whole-change review with gpt-6.1-sol high (user-requested sol). Supply spec, plan, base `62ab91c`, final HEAD and verification results.
- [ ] Verify findings against code; fix real critical/important issues with reproducing RED→GREEN tests. Repeat affected checks and full suite after changes.
- [ ] Audit every spec scenario against actual code/tests, record final limitations and review results, commit and leave the branch ready for review. No merge/push/deploy requested.
