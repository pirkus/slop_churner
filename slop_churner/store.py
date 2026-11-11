"""SQLite persistence for runs, tasks and per-step logs.

The web UI is the primary consumer, but the store is deliberately UI-free:
it just maps slop-churner's event stream (see engine.run/runner.run_task `sink`)
into a small local database so history survives restarts.

DB location: $SLOP_CHURNER_DB, else ~/.slop-churner/slop-churner.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path() -> Path:
    return Path(os.environ.get("SLOP_CHURNER_DB") or "~/.slop-churner/slop-churner.db").expanduser()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    -- Approved plan for this run: the test_cmd the planner settled on
    -- (see apply_event "planned"). Stored as its own column so plans can
    -- be edited/replaced per run without touching the immutable `params`.
    plan_test_cmd TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    iterations  INTEGER NOT NULL DEFAULT 0,
    branch      TEXT,
    worktree    TEXT,
    last_error  TEXT,
    summary     TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id     INTEGER NOT NULL,
    iteration   INTEGER NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL,
    ok          INTEGER,
    output      TEXT,
    duration    REAL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps (run_id, task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks (run_id);
"""


class Store:
    """Thread-safe wrapper over a local SQLite database."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or default_db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Migration for databases created before plan_test_cmd existed:
            # CREATE TABLE IF NOT EXISTS leaves old runs tables untouched, so
            # add the column if it is missing.
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)")}
            if "plan_test_cmd" not in cols:
                self._conn.execute("ALTER TABLE runs ADD COLUMN plan_test_cmd TEXT")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- runs ---------------------------------------------------------------

    def create_run(self, repo: str, prompt: str, params: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (repo, prompt, params, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (str(repo), prompt, json.dumps(params), _now(), _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def set_run_status(self, run_id: int, status: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, error = COALESCE(?, error), updated_at = ? WHERE id = ?",
                (status, error, _now(), run_id),
            )
            self._conn.commit()

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ?, error = COALESCE(?, error), "
                "updated_at = ?, finished_at = ? WHERE id = ?",
                (status, error, _now(), _now(), run_id),
            )
            self._conn.commit()

    def set_plan_test_cmd(self, run_id: int, test_cmd: str | None) -> None:
        """Store (or clear) the approved plan's test_cmd for a run."""
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET plan_test_cmd = ?, updated_at = ? WHERE id = ?",
                (test_cmd, _now(), run_id),
            )
            self._conn.commit()

    def get_plan(self, run_id: int) -> dict[str, Any] | None:
        """Return the stored plan for a run: ``{test_cmd, tasks}``.

        ``tasks`` is the current ordered task list; ``test_cmd`` is the
        approved plan command (None if no plan has been recorded yet).
        Returns None if the run does not exist.
        """
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute(
                "SELECT plan_test_cmd FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            tasks = [dict(r) for r in self._conn.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY task_id", (run_id,)
            ).fetchall()]
            return {"test_cmd": row["plan_test_cmd"], "tasks": tasks}

    def list_runs(self) -> list[sqlite3.Row]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            return self._conn.execute(
                """
                SELECT r.*,
                       (SELECT COUNT(*) FROM tasks t WHERE t.run_id = r.id) AS n_tasks,
                       (SELECT COUNT(*) FROM tasks t WHERE t.run_id = r.id AND t.status = 'done') AS n_done
                FROM runs r ORDER BY r.id DESC
                """
            ).fetchall()

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["params"] = json.loads(d["params"])
            return d

    # -- tasks --------------------------------------------------------------

    def _upsert_task(self, run_id: int, t: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO tasks (run_id, task_id, title, description, status, iterations,
                               branch, worktree, last_error, summary, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, task_id) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                status = excluded.status,
                iterations = excluded.iterations,
                branch = COALESCE(excluded.branch, tasks.branch),
                worktree = COALESCE(excluded.worktree, tasks.worktree),
                last_error = COALESCE(excluded.last_error, tasks.last_error),
                summary = COALESCE(excluded.summary, tasks.summary),
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                int(t["id"]),
                t.get("title", ""),
                t.get("description", ""),
                t.get("status", "pending"),
                int(t.get("iterations", 0)),
                t.get("branch"),
                t.get("worktree"),
                t.get("last_error"),
                t.get("summary"),
                _now(),
            ),
        )

    def upsert_task(self, run_id: int, t: dict[str, Any]) -> None:
        with self._lock:
            self._upsert_task(run_id, t)
            self._conn.commit()

    def replace_tasks(self, run_id: int, tasks: list[dict[str, Any]]) -> None:
        """Replace the run's whole task list with a fresh ordered one.

        Deletes every existing task row for the run, then inserts the given
        tasks in order (each dict needs ``task_id``, ``title``,
        ``description``; ``task_id`` is the ordering key). Reuses
        ``_upsert_task`` so new rows keep the standard defaults.
        """
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE run_id = ?", (run_id,))
            for t in tasks:
                self._upsert_task(run_id, {
                    "id": int(t["task_id" if "task_id" in t else t["id"]]),
                    "title": t.get("title", ""),
                    "description": t.get("description", ""),
                })
            self._conn.commit()

    def get_tasks(self, run_id: int) -> list[dict[str, Any]]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY task_id", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # -- steps --------------------------------------------------------------

    def add_step(self, run_id: int, s: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO steps (run_id, task_id, iteration, stage, ok, output, duration, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    int(s["task_id"]),
                    int(s.get("iteration", 0)),
                    s.get("stage", ""),
                    None if s.get("ok") is None else int(bool(s["ok"])),
                    s.get("output"),
                    s.get("duration"),
                    _now(),
                ),
            )
            self._conn.commit()

    def get_steps(self, run_id: int, task_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            if task_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM steps WHERE run_id = ? ORDER BY id", (run_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM steps WHERE run_id = ? AND task_id = ? ORDER BY id",
                    (run_id, task_id),
                ).fetchall()
            return [dict(r) for r in rows]

    # -- event stream ---------------------------------------------------------

    def apply_event(self, run_id: int, name: str, data: dict[str, Any]) -> None:
        """Fold one engine event into the database."""
        with self._lock:
            if name == "planned":
                # Persist the approved plan: its test_cmd plus the task list.
                if data.get("test_cmd") is not None:
                    self._conn.execute(
                        "UPDATE runs SET plan_test_cmd = ? WHERE id = ?",
                        (data["test_cmd"], run_id),
                    )
                for t in data.get("tasks", []):
                    self._upsert_task(run_id, t)
            elif name == "task_updated":
                self._upsert_task(run_id, data)
            elif name == "agent_output":
                # Live-streamed agent stdout lines: only forwarded to SSE
                # subscribers, deliberately not persisted (the final 'step'
                # event carries the full output).
                pass
            elif name == "step":
                self._conn.execute(
                    "INSERT INTO steps (run_id, task_id, iteration, stage, ok, output, duration, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        int(data["task_id"]),
                        int(data.get("iteration", 0)),
                        data.get("stage", ""),
                        None if data.get("ok") is None else int(bool(data["ok"])),
                        data.get("output"),
                        data.get("duration"),
                        _now(),
                    ),
                )
            self._conn.execute(
                "UPDATE runs SET updated_at = ? WHERE id = ?", (_now(), run_id)
            )
            self._conn.commit()
