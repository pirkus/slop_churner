"""Offline test for slop_churner.store archive support and migration.

Run:  python3 tests/store_test.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slop_churner.store import Store  # noqa: E402


def check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)
    print(f"ok - {name}")


def make_old_schema_db(path: Path) -> None:
    """Create a database with the pre-archived schema (no plan_test_cmd either)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        repo        TEXT NOT NULL,
        prompt      TEXT NOT NULL,
        params      TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL DEFAULT 'queued',
        error       TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        finished_at TEXT
    );
    INSERT INTO runs (repo, prompt, params, created_at, updated_at)
    VALUES ('repo', 'prompt', '{}', 't', 't');
    INSERT INTO runs (repo, prompt, params, created_at, updated_at)
    VALUES ('repo', 'prompt', '{}', 't', 't');
    """)
    conn.commit()
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="slop-churner-store-test-"))

    # Migration: a pre-existing database (created before archived existed)
    # must open cleanly and gain the column.
    old_db = tmp / "old.db"
    make_old_schema_db(old_db)
    s = Store(old_db)
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(runs)")}
    check("migrated db has archived column", "archived" in cols)
    check("migrated db keeps old rows", len(s.list_runs()) == 2)

    # list_runs() omits archived runs by default.
    s.set_run_archived(1, True)
    check("new runs default to archived=0", s.get_run(2)["archived"] == 0)
    check("list_runs() omits archived", [r["id"] for r in s.list_runs()] == [2])
    check(
        "list_runs(include_archived=True) includes archived",
        sorted(r["id"] for r in s.list_runs(include_archived=True)) == [1, 2],
    )
    archived = {r["id"]: r["archived"] for r in s.list_runs(include_archived=True)}
    check("archived flag round-trips", archived == {1: 1, 2: 0})

    # Un-archiving puts the run back in the default listing.
    s.set_run_archived(1, False)
    check("un-archiving restores the run", sorted(r["id"] for r in s.list_runs()) == [1, 2])
    s.close()

    # A freshly created database gets the column from _SCHEMA.
    fresh = Store(tmp / "fresh.db")
    cols = {r[1] for r in fresh._conn.execute("PRAGMA table_info(runs)")}
    check("fresh db has archived column", "archived" in cols)
    fresh.close()

    print("STORE TEST PASSED")


if __name__ == "__main__":
    main()
