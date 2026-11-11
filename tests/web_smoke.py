"""End-to-end test for the web UI/API: creates runs over HTTP with a stubbed
agent and verifies runs, tasks and step logs land in the SQLite store.

Run:  python3 tests/web_smoke.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests.smoke as smoke  # reuse repo fixture + plan JSON  # noqa: E402
from ralph import planner, runner  # noqa: E402
from ralph.agent import AgentResult  # noqa: E402
from ralph.config import Config  # noqa: E402
from ralph.web import Handler, ServerContext  # noqa: E402


def sh(cwd, *a):
    smoke.sh(cwd, *a)


def make_repo(root: Path) -> None:
    smoke.make_repo(root)


class StubAgent:
    """Planner returns JSON; implementer edits a file. Tests flip once (red -> green)."""

    def __init__(self):
        self.test_attempts = 0
        self.fail_always = False

    def __call__(self, cfg, prompt, *, cwd, tools=None, timeout=None, **kw):
        time.sleep(0.2)  # give the SSE client a window to observe a live event
        if "planning stage" in prompt:
            return AgentResult(True, smoke.PLAN_JSON, 0, 0.2)
        app = cwd / "app.py"
        if "subtract" not in app.read_text():
            app.write_text(
                "def add(a, b):\n    return a + b\n\n"
                "def subtract(a, b):\n    return a - b\n"
            )
        return AgentResult(True, "implemented subtract", 0, 0.2)


def flaky_tests(cfg, wt, test_cmd):
    flaky_tests.n = getattr(flaky_tests, "n", 0) + 1
    if stub.fail_always:
        return runner.StageResult(False, "always failing: " + "x" * 40, 1)
    if flaky_tests.n == 1:
        return runner.StageResult(False, "AssertionError: subtract missing", 1)
    return runner.StageResult(True, "2 passed", 0)


def api_call(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def wait_for(url: str, key: str, values, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = api_call("GET", url)
        if d.get(key) in values:
            return d
        time.sleep(0.2)
    raise TimeoutError(f"{key} never reached {values}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ralph-web-smoke-"))
    repo = tmp / "proj"
    repo.mkdir()
    make_repo(repo)

    # Stub the agent + tests (patch where they are looked up at call time).
    global stub
    stub = StubAgent()
    planner.run_agent = stub
    runner.run_agent = stub
    runner.run_tests = flaky_tests

    # Boot the server in-process on a random port.
    db = tmp / "ralph.db"
    ctx = ServerContext(db)
    Handler.ctx = ctx
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # -- health -------------------------------------------------------------
    h = api_call("GET", f"{base}/api/health")
    assert h["ok"] and str(db) in h["db"], h

    # -- index page ----------------------------------------------------------
    with urllib.request.urlopen(f"{base}/") as r:
        page = r.read().decode()
    assert "<form id=\"newrun\">" in page and "EventSource" in page

    # -- create run 1 (green after one fix loop) ------------------------------
    r1 = api_call("POST", f"{base}/api/runs", {
        "prompt": "add a subtract function",
        "repo": str(repo),
        "base": "main",
        "test_cmd": "true",
        "max_iterations": 3,
    })
    rid = r1["run_id"]
    assert rid >= 1

    # -- SSE: subscribe and make sure live events actually stream ---------------
    conn = HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", f"/api/runs/{rid}/events")
    resp = conn.getresponse()
    assert resp.status == 200 and "text/event-stream" in resp.getheader("Content-Type")
    seen = []
    deadline = time.time() + 20
    while time.time() < deadline:
        line = resp.fp.readline()
        if not line:
            break
        if line.startswith(b"data: "):
            seen.append(json.loads(line[6:]))
        if any(e["name"] == "step" for e in seen):
            break
    conn.close()
    assert any(e["name"] == "task_updated" for e in seen), seen
    assert any(e["name"] == "step" for e in seen), seen

    run = wait_for(f"{base}/api/runs/{rid}", "status", {"done", "error", "failed"})
    assert run["status"] == "done", run
    assert len(run["tasks"]) == 1
    t1 = run["tasks"][0]
    assert t1["status"] == "done"
    assert t1["iterations"] == 2, "expected implement->fail->fix->pass"

    # -- step logs ------------------------------------------------------------
    task = api_call("GET", f"{base}/api/runs/{rid}/tasks/{t1['task_id']}")
    stages = [(s["stage"], bool(s["ok"])) for s in task["steps"]]
    assert ("implement", True) in stages, stages
    assert ("test", False) in stages, stages
    assert ("fix", True) in stages, stages
    assert ("test", True) in stages, stages
    assert ("pr", True) in stages, stages
    test_fail = next(s for s in task["steps"]
                     if s["stage"] == "test" and not s["ok"])
    assert "AssertionError" in test_fail["output"], test_fail["output"]
    assert all(s["duration"] is not None for s in task["steps"])

    # -- the change landed in the repo ---------------------------------------
    assert "def subtract" in (repo / "app.py").read_text()

    # -- runs list -------------------------------------------------------------
    lst = api_call("GET", f"{base}/api/runs")["runs"]
    assert any(r["id"] == rid and r["n_done"] == 1 and r["n_tasks"] == 1 for r in lst), lst

    # -- store plan API: get_plan / replace_tasks -------------------------------
    # Migration: an old-schema DB (no plan_test_cmd) must gain the column.
    from ralph.store import Store
    old_db = tmp / "old-schema.db"
    import sqlite3 as _s3
    _c = _s3.connect(str(old_db))
    _c.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "repo TEXT NOT NULL, prompt TEXT NOT NULL, "
               "params TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, "
               "error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
               "finished_at TEXT)")
    _c.execute("INSERT INTO runs (repo, prompt, params, status, created_at, updated_at) "
               "VALUES ('r', 'p', '{}', 'queued', 't', 't')")
    _c.commit(); _c.close()
    _s = Store(old_db)
    old_cols = {r[1] for r in _s._conn.execute("PRAGMA table_info(runs)")}
    assert "plan_test_cmd" in old_cols, old_cols
    _s.set_plan_test_cmd(1, "pytest -q")
    assert _s.get_plan(1) == {"test_cmd": "pytest -q", "tasks": []}
    _s.close()

    plan = ctx.store.get_plan(rid)
    assert plan is not None and plan["test_cmd"] == "true", plan
    assert [t["task_id"] for t in plan["tasks"]] == [1]
    assert ctx.store.get_plan(999999) is None

    new_tasks = [
        {"task_id": 7, "title": "fresh one", "description": "d7"},
        {"task_id": 9, "title": "fresh two", "description": "d9"},
    ]
    ctx.store.replace_tasks(rid, new_tasks)
    replaced = ctx.store.get_tasks(rid)
    assert [t["task_id"] for t in replaced] == [7, 9], replaced
    assert [t["title"] for t in replaced] == ["fresh one", "fresh two"]
    assert all(t["status"] == "pending" for t in replaced)
    plan = ctx.store.get_plan(rid)
    assert [t["task_id"] for t in plan["tasks"]] == [7, 9]
    # restore the original task list so the run stays consistent
    ctx.store.replace_tasks(rid, [{"task_id": t1["task_id"], "title": t1["title"],
                                   "description": t1["description"]}])
    assert [t["task_id"] for t in ctx.store.get_tasks(rid)] == [1]

    # -- validation errors ------------------------------------------------------
    try:
        api_call("POST", f"{base}/api/runs", {"prompt": "", "repo": str(repo)})
        raise AssertionError("expected 400 for empty prompt")
    except urllib.error.HTTPError as e:
        assert e.code == 400

    # -- run 2: failing task -> retry ------------------------------------------
    stub.fail_always = True
    r2 = api_call("POST", f"{base}/api/runs", {
        "prompt": "add a subtract function",
        "repo": str(repo),
        "base": "main",
        "test_cmd": "true",
        "max_iterations": 2,
    })
    rid2 = r2["run_id"]
    run2 = wait_for(f"{base}/api/runs/{rid2}", "status", {"done", "error", "failed"})
    assert run2["status"] == "failed", run2
    assert run2["tasks"][0]["status"] == "failed"
    assert "always failing" in run2["tasks"][0]["last_error"]

    # retry the failed task with tests back to green
    stub.fail_always = False
    flaky_tests.n = 0
    api_call("POST", f"{base}/api/runs/{rid2}/retry/{run2['tasks'][0]['task_id']}")
    run2 = wait_for(f"{base}/api/runs/{rid2}", "status", {"done", "error"})
    assert run2["status"] == "done", run2
    assert run2["tasks"][0]["status"] == "done"

    # -- cancel endpoint on a finished run must 409 ------------------------------
    try:
        api_call("POST", f"{base}/api/runs/{rid}/cancel")
        raise AssertionError("expected 409")
    except urllib.error.HTTPError as e:
        assert e.code == 409

    # -- DB file actually on disk -------------------------------------------------
    assert db.exists()
    import sqlite3
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] >= 11
    conn.close()

    httpd.shutdown()
    print("\nWEB SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
