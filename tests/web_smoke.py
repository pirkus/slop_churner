"""End-to-end test for the web UI/API: creates runs over HTTP with a stubbed
agent and verifies the plan-review flow (create -> awaiting_approval ->
edit plan -> approve -> execute), plus tasks, step logs and SSE events land
correctly in the SQLite store.

Run:  python3 tests/web_smoke.py
"""
from __future__ import annotations

import json
import subprocess
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
from slop_churner import planner, runner  # noqa: E402
from slop_churner.agent import AgentResult  # noqa: E402
from slop_churner.config import Config  # noqa: E402
from slop_churner.web import Handler, ServerContext, _H2  # noqa: E402

if _H2:  # boot the dual h2c/HTTP1.1 server when h2 is available
    from slop_churner.web import _DualRequestHandler, _ThreadingTCPServer  # noqa: E402


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
        # Simulate streaming agent output: emit a few lines while the
        # "agent" is still running (i.e. before this call returns).
        on_output = kw.get("on_output")
        if on_output is not None:
            for i in range(1, 4):
                on_output(f"stub agent line {i}\n")
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


def expect_error(method: str, url: str, code: int, body: dict | None = None) -> None:
    try:
        api_call(method, url, body)
    except urllib.error.HTTPError as e:
        assert e.code == code, f"expected {code}, got {e.code}"
        return
    raise AssertionError(f"expected HTTP {code} for {method} {url}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="slop-churner-web-smoke-"))
    repo = tmp / "proj"
    repo.mkdir()
    make_repo(repo)

    # Stub the agent + tests (patch where they are looked up at call time).
    global stub
    stub = StubAgent()
    planner.run_agent = stub
    runner.run_agent = stub
    runner.run_tests = flaky_tests

    # Boot the server in-process on a random port. With h2 installed this is
    # the dual-protocol (h2c + HTTP/1.1) server, so every HTTP/1.1 assertion
    # below also exercises the preface sniffing + fallback path.
    db = tmp / "slop-churner.db"
    ctx = ServerContext(db)
    Handler.ctx = ctx
    if _H2:
        httpd = _ThreadingTCPServer(("127.0.0.1", 0), _DualRequestHandler)
    else:
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
    # Plan review UI: awaiting_approval must render an editable plan with
    # add/remove-task, an optional test cmd, approve (PUT /plan + approve)
    # and reject actions, and the SSE handler must refresh on 'planned'.
    for needle in (
        "awaiting_approval", "planReview", "Add task", "Approve and start",
        "Reject", "remove", 'plan_test_cmd', 'method: "PUT"',
        "/plan", "/approve", "/reject", 'name === "planned"',
        # acceptance gate UI
        "awaiting_acceptance", "diffReview", "Accept", "/diff",
        'doAction("accept")', 'doAction("discard")', "Reject",
        # transcript pane (persisted history + live streaming) + SSE handler
        "transcript", "tstep", "/transcript", 'name === "agent_output"',
    ):
        assert needle in page, f"ui.html is missing {needle!r}"

    # -- create run 1: parks in awaiting_approval, nothing executes ----------
    r1 = api_call("POST", f"{base}/api/runs", {
        "prompt": "add a subtract function",
        "repo": str(repo),
        "base": "main",
        "test_cmd": "true",
        "max_iterations": 3,
    })
    rid = r1["run_id"]
    assert rid >= 1
    run = wait_for(f"{base}/api/runs/{rid}", "status", {"awaiting_approval", "error"})
    assert run["status"] == "awaiting_approval", run
    assert len(run["tasks"]) == 1
    t1 = run["tasks"][0]
    assert t1["title"] == "Add a subtract function"
    assert t1["status"] == "pending" and t1["iterations"] == 0, t1
    # No task may have executed while awaiting approval.
    assert "def subtract" not in (repo / "app.py").read_text()

    # -- edit the plan while awaiting approval (PUT /plan) --------------------
    edited = {
        "tasks": [
            {"task_id": 1, "title": "Add a subtract function (edited)",
             "description": "Add subtract(a, b) to app.py returning a - b, with a test."},
            {"task_id": 2, "title": "Add a multiply function",
             "description": "Add multiply(a, b) to app.py returning a * b."},
        ],
        "test_cmd": "true",
    }
    api_call("PUT", f"{base}/api/runs/{rid}/plan", edited)
    run = api_call("GET", f"{base}/api/runs/{rid}")
    assert run["status"] == "awaiting_approval", run
    assert [t["task_id"] for t in run["tasks"]] == [1, 2], run["tasks"]
    assert run["tasks"][0]["title"] == "Add a subtract function (edited)"
    assert run["tasks"][1]["title"] == "Add a multiply function"
    assert all(t["status"] == "pending" and t["iterations"] == 0 for t in run["tasks"])
    assert ctx.store.get_plan(rid)["test_cmd"] == "true"
    # Still nothing executed.
    assert "def subtract" not in (repo / "app.py").read_text()

    # -- plan edit validation (while still awaiting approval) ------------------
    expect_error("PUT", f"{base}/api/runs/{rid}/plan", 400, {"tasks": []})
    expect_error("PUT", f"{base}/api/runs/{rid}/plan", 400,
                 {"tasks": [{"task_id": 1, "title": "x"}]})
    expect_error("PUT", f"{base}/api/runs/{rid}/plan", 400,
                 {"tasks": [{"task_id": 1, "title": "x", "description": "d"}],
                  "test_cmd": 42})

    # -- SSE: subscribe, then approve; live events must stream ----------------
    conn = HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", f"/api/runs/{rid}/events")
    resp = conn.getresponse()
    assert resp.status == 200 and "text/event-stream" in resp.getheader("Content-Type")
    threading.Thread(
        target=lambda: api_call("POST", f"{base}/api/runs/{rid}/approve"),
        daemon=True,
    ).start()
    seen = []
    deadline = time.time() + 30
    while time.time() < deadline:
        line = resp.fp.readline()
        if not line:
            break
        if line.startswith(b"data: "):
            seen.append(json.loads(line[6:]))
        if any(e["name"] == "closed" for e in seen):
            break
    conn.close()
    names = [e["name"] for e in seen]
    for expected in ("planned", "agent_output", "task_updated", "step",
                     "run_review", "closed"):
        assert expected in names, f"missing {expected} in SSE stream: {names}"
    # agent_output lines must be delivered WHILE the agent is still running:
    # the first implement agent_output precedes the implement 'step' event
    # (the step only fires once the agent call has returned).
    ao_idx = [i for i, e in enumerate(seen)
              if e["name"] == "agent_output" and e["data"].get("stage") == "implement"]
    assert ao_idx, f"no implement agent_output in SSE stream: {names}"
    step_idx = [i for i, e in enumerate(seen)
                if e["name"] == "step" and e["data"].get("stage") == "implement"]
    assert step_idx, f"no implement step in SSE stream: {names}"
    assert ao_idx[0] < step_idx[0], \
        f"agent_output ({ao_idx[0]}) must arrive before step ({step_idx[0]}): {names}"
    ao = seen[ao_idx[0]]["data"]
    assert ao["task_id"] == 1 and ao["line"].startswith("stub agent line"), ao

    # -- transcript: agent output is persisted and replayable (reload-safe) --
    tr = api_call("GET", f"{base}/api/runs/{rid}/transcript")
    tsteps = tr["steps"]
    assert tsteps, "transcript must have steps"
    assert tsteps[0]["key"] == "planning", tsteps[0]
    assert "stub agent line 3" in tsteps[0]["text"], tsteps[0]
    impl = [s for s in tsteps if s["key"] == "1:implement"]
    assert impl and "stub agent line 1" in impl[0]["text"], tsteps
    assert impl[0]["label"] == "task 1 / implement", impl

    # The run parks in awaiting_acceptance: work is staged, base untouched.
    run = wait_for(f"{base}/api/runs/{rid}", "status",
                   {"awaiting_acceptance", "error", "failed"})
    assert run["status"] == "awaiting_acceptance", run
    assert [t["task_id"] for t in run["tasks"]] == [1, 2]
    t1 = run["tasks"][0]
    assert t1["title"] == "Add a subtract function (edited)", t1
    assert t1["status"] == "done"
    assert t1["iterations"] == 2, "expected implement->fail->fix->pass"
    t2 = run["tasks"][1]
    assert t2["status"] == "done"
    assert "def subtract" not in (repo / "app.py").read_text(), \
        "base branch must stay clean while the run awaits acceptance"

    # -- diff endpoint --------------------------------------------------------
    d = api_call("GET", f"{base}/api/runs/{rid}/diff")
    assert d["run_branch"] == f"slop-churner/run-{rid}", d
    assert d["base_branch"] == "main", d
    assert d["added"] > 0 and d["deleted"] >= 0, d
    assert any(f["path"] == "app.py" and f["added"] > 0 for f in d["files"]), d
    assert any(f["patch"] and "def subtract" in f["patch"] for f in d["files"]), d
    assert d["commits"], d
    assert [t["task_id"] for t in d["tasks"]] == [1, 2], d

    # -- accept: work lands in base, staging branch cleaned up ----------------
    api_call("POST", f"{base}/api/runs/{rid}/accept")
    run = wait_for(f"{base}/api/runs/{rid}", "status", {"accepted", "error"})
    assert run["status"] == "accepted", run
    assert "def subtract" in (repo / "app.py").read_text(), \
        "staged changes should be in main after accept"
    branches = subprocess.run(["git", "branch"], cwd=str(repo),
                              capture_output=True, text=True).stdout
    assert "slop-churner/" not in branches, f"branches should be cleaned up:\n{branches}"
    # Accepting a second time must 409.
    expect_error("POST", f"{base}/api/runs/{rid}/accept", 409)
    expect_error("POST", f"{base}/api/runs/{rid}/discard", 409)
    # diff for a cleaned-up run 404s (no staging branch)
    expect_error("GET", f"{base}/api/runs/{rid}/diff", 404)

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
    assert any(r["id"] == rid and r["n_done"] == 2 and r["n_tasks"] == 2
               and r["status"] == "accepted" for r in lst), lst

    # -- plan review endpoints reject once the run is no longer awaiting -------
    expect_error("PUT", f"{base}/api/runs/{rid}/plan", 409, edited)
    expect_error("POST", f"{base}/api/runs/{rid}/approve", 409)
    expect_error("POST", f"{base}/api/runs/{rid}/reject", 409)

    # -- store plan API: get_plan / replace_tasks -------------------------------
    # Migration: an old-schema DB (no plan_test_cmd) must gain the column.
    from slop_churner.store import Store
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
    assert [t["task_id"] for t in plan["tasks"]] == [1, 2]
    assert ctx.store.get_plan(999999) is None

    cur_tasks = ctx.store.get_tasks(rid)
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
    ctx.store.replace_tasks(rid, [{"task_id": t["task_id"], "title": t["title"],
                                   "description": t["description"]} for t in cur_tasks])
    assert [t["task_id"] for t in ctx.store.get_tasks(rid)] == [1, 2]

    # -- validation errors ------------------------------------------------------
    try:
        api_call("POST", f"{base}/api/runs", {"prompt": "", "repo": str(repo)})
        raise AssertionError("expected 400 for empty prompt")
    except urllib.error.HTTPError as e:
        assert e.code == 400

    # -- run 2: failing task -> approve -> fail -> retry ------------------------
    stub.fail_always = True
    r2 = api_call("POST", f"{base}/api/runs", {
        "prompt": "add a subtract function",
        "repo": str(repo),
        "base": "main",
        "test_cmd": "true",
        "max_iterations": 2,
    })
    rid2 = r2["run_id"]
    run2 = wait_for(f"{base}/api/runs/{rid2}", "status", {"awaiting_approval", "error"})
    assert run2["status"] == "awaiting_approval", run2
    # Retrying before approval must be refused.
    expect_error("POST", f"{base}/api/runs/{rid2}/retry/{run2['tasks'][0]['task_id']}", 409)
    api_call("POST", f"{base}/api/runs/{rid2}/approve")
    run2 = wait_for(f"{base}/api/runs/{rid2}", "status", {"done", "error", "failed"})
    assert run2["status"] == "failed", run2
    assert run2["tasks"][0]["status"] == "failed"
    assert "always failing" in run2["tasks"][0]["last_error"]

    # retry the failed task with tests back to green (reuses the existing plan)
    stub.fail_always = False
    flaky_tests.n = 0
    api_call("POST", f"{base}/api/runs/{rid2}/retry/{run2['tasks'][0]['task_id']}")
    run2 = wait_for(f"{base}/api/runs/{rid2}", "status",
                    {"awaiting_acceptance", "error"})
    assert run2["status"] == "awaiting_acceptance", run2
    assert run2["tasks"][0]["status"] == "done"
    # Retrying while awaiting acceptance must be refused.
    expect_error("POST", f"{base}/api/runs/{rid2}/retry/{run2['tasks'][0]['task_id']}", 409)

    # -- discard: staging work is thrown away ---------------------------------
    api_call("POST", f"{base}/api/runs/{rid2}/discard")
    run2 = api_call("GET", f"{base}/api/runs/{rid2}")
    assert run2["status"] == "rejected", run2
    branches = subprocess.run(["git", "branch"], cwd=str(repo),
                              capture_output=True, text=True).stdout
    assert f"slop-churner/run-{rid2}" not in branches, branches
    wts = subprocess.run(["git", "worktree", "list"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    assert wts.count("\n") <= 1, f"worktrees should be cleaned up:\n{wts}"

    # -- run 3: reject the plan -> cancelled, nothing executed -------------------
    r3 = api_call("POST", f"{base}/api/runs", {
        "prompt": "add a subtract function",
        "repo": str(repo),
        "base": "main",
        "test_cmd": "true",
    })
    rid3 = r3["run_id"]
    run3 = wait_for(f"{base}/api/runs/{rid3}", "status", {"awaiting_approval", "error"})
    assert run3["status"] == "awaiting_approval", run3
    api_call("POST", f"{base}/api/runs/{rid3}/reject")
    run3 = api_call("GET", f"{base}/api/runs/{rid3}")
    assert run3["status"] == "cancelled", run3
    assert run3["tasks"][0]["status"] == "pending" and run3["tasks"][0]["iterations"] == 0
    assert run3["running"] is False

    # -- run 4: repo claim was released by the rejected run ----------------------
    r4 = api_call("POST", f"{base}/api/runs", {
        "prompt": "add a subtract function",
        "repo": str(repo),
        "base": "main",
        "test_cmd": "true",
    })
    rid4 = r4["run_id"]
    assert api_call("GET", f"{base}/api/runs/{rid4}")["status"] in (
        "planning", "awaiting_approval", "error"), "repo claim should be free"
    wait_for(f"{base}/api/runs/{rid4}", "status", {"awaiting_approval", "error"})
    api_call("POST", f"{base}/api/runs/{rid4}/reject")

    # -- cancel endpoint on a finished run must 409 ------------------------------
    try:
        api_call("POST", f"{base}/api/runs/{rid}/cancel")
        raise AssertionError("expected 409")
    except urllib.error.HTTPError as e:
        assert e.code == 409

    # -- cleartext HTTP/2 (h2c) on the same port ---------------------------------
    # NOTE: must run before the run-count assertions below (SSE check creates
    # a run row).
    if _H2:
        import tests.http2_smoke as h2smoke
        h2smoke.h2_checks(ctx, port, db)
    else:
        print("\nh2 not installed; skipping HTTP/2 checks")

    # -- DB file actually on disk -------------------------------------------------
    assert db.exists()
    import sqlite3
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] >= 11
    conn.close()

    httpd.shutdown()
    print("\nWEB SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
