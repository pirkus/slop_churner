"""Web UI + JSON API for slop-churner.

Serves a single-page dashboard where you can:
  - create a run from a prompt + tunable parameters,
  - review and edit the proposed plan, then approve or reject it,
  - watch jobs progress live (SSE),
  - browse every step's log (implement/fix, test, pr),
  - cancel a running job or retry a failed task.

Stdlib http.server + sqlite3, plus the optional `h2` package for cleartext
HTTP/2 (h2c, prior-knowledge on the same port). If `h2` is not installed the
server transparently falls back to HTTP/1.1 only.

    slop-churner web [--host 127.0.0.1] [--port 8787] [--db PATH] [--open] [--no-http2]
"""
from __future__ import annotations

import json
import queue
import re
import socket
import socketserver
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Optional HTTP/2 support: the project is otherwise stdlib-only, so the
# whole h2c path is gated behind this import and degrades to HTTP/1.1.
try:
    import h2.config
    import h2.connection
    import h2.events
    from h2.exceptions import H2Error
    _H2 = True
    # h2 >= 4.2 emits RequestReceived; older 4.x emits the legacy Headers
    # event. Both expose .stream_id/.headers/.stream_ended identically.
    _H2_REQ_EVENT = (getattr(h2.events, "RequestReceived", None)
                     or h2.events.Headers)
except ImportError:  # h2 not installed -> HTTP/1.1 only
    _H2 = False

from . import __version__, engine, events, gitutil, planner
from .config import Config
from .errors import SlopChurnerError
from .state import State
from .store import Store, default_db_path

# RFC 7540 client connection preface ("prior knowledge" h2c detection).
_H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

# ---------------------------------------------------------------------------
# Live-event fan-out for SSE
# ---------------------------------------------------------------------------

class Hub:
    """Fan-out of engine events to any number of SSE subscribers per run."""

    def __init__(self) -> None:
        self._subs: dict[int, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, run_id: int) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: int, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(run_id)
            if subs and q in subs:
                subs.remove(q)
            if subs is not None and not subs:
                del self._subs[run_id]

    def publish(self, run_id: int, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(run_id, ()))
        for q in subs:
            q.put(event)


class WebSink:
    """events.Sink that persists to the store and pushes live to subscribers."""

    def __init__(self, store: Store, hub: Hub, run_id: int) -> None:
        self.store = store
        self.hub = hub
        self.run_id = run_id

    def __call__(self, name: str, data: dict[str, Any]) -> None:
        try:
            self.store.apply_event(self.run_id, name, data)
        except Exception as exc:  # never let DB hiccups kill the run
            print(f"slop-churner web: store error: {exc}", file=sys.stderr)
        self.hub.publish(self.run_id, {"name": name, "data": data})


# ---------------------------------------------------------------------------
# Run workers
# ---------------------------------------------------------------------------

def _build_cfg(params: dict[str, Any]) -> Config:
    def _int(key: str, default: int | None) -> int | None:
        v = params.get(key)
        if v in (None, ""):
            return default
        return int(v)

    return Config(
        repo=Path(params.get("repo") or "."),
        base_branch=params.get("base") or None,
        agent_cmd=params.get("agent") or "pi",
        model=params.get("model") or None,
        max_iterations=_int("max_iterations", 3),
        max_tasks=_int("max_tasks", 8),
        budget=_int("budget", None),
        test_cmd=params.get("test_cmd") or None,
        agent_timeout=_int("agent_timeout", 1800),
        test_timeout=_int("test_timeout", 900),
    )


def _planning_worker(ctx: "ServerContext", run_id: int, params: dict[str, Any]) -> None:
    """Planning-only worker: build the plan for review, never execute a task.

    Ends in 'awaiting_approval' (awaiting explicit user approval) or
    'error'. Releases the repo claim either way.
    """
    ctx.workers[run_id] = threading.Event()
    try:
        ctx.store.set_run_status(run_id, "planning")
        cfg = _build_cfg(params)
        # Stream the planner's live output to the UI over SSE (agent_output
        # is live-only; the store ignores it).
        sink = WebSink(ctx.store, ctx.hub, run_id)
        plan = planner.plan(cfg, params.get("prompt", ""), sink=sink)
        for t in plan.tasks:
            ctx.store.upsert_task(run_id, t.to_dict())
        ctx.store.set_plan_test_cmd(run_id, plan.test_cmd)
        ctx.hub.publish(run_id, {"name": "planned", "data": {
            "tasks": [t.to_dict() for t in plan.tasks],
            "test_cmd": plan.test_cmd,
        }})
        ctx.store.set_run_status(run_id, "awaiting_approval")
    except (SlopChurnerError, ValueError, OSError) as exc:
        ctx.hub.publish(run_id, {"name": "error", "data": {"message": str(exc)}})
        ctx.store.finish_run(run_id, "error", str(exc))
    except Exception as exc:  # unexpected
        ctx.hub.publish(run_id, {"name": "error", "data": {"message": f"internal error: {exc}"}})
        ctx.store.finish_run(run_id, "error", f"internal error: {exc}")
    finally:
        ctx.workers.pop(run_id, None)
        ctx.release_repo(params.get("repo") or ".", run_id)


def _worker(ctx: "ServerContext", run_id: int, params: dict[str, Any],
            only_task: int | None = None) -> None:
    """Execution worker: runs the stored (approved) plan for a run.

    A fresh execution uses the plan stored during the review stage;
    a retry reuses the existing plan/state and runs a single task.
    """
    cancel = threading.Event()
    ctx.workers[run_id] = cancel
    sink = WebSink(ctx.store, ctx.hub, run_id)

    def _close(status: str, error: str | None) -> None:
        ctx.store.finish_run(run_id, status, error)
        ctx.hub.publish(run_id, {"name": "closed", "data": {"status": status, "error": error}})

    try:
        ctx.store.set_run_status(run_id, "running")
        cfg = _build_cfg(params)
        # This run's work is staged on its own branch; the base branch is
        # only touched when the user accepts the finished run.
        cfg.run_branch = gitutil.run_branch_name(run_id)
        plan = None
        if only_task is None:
            # Fresh execution: build the plan from the stored, approved
            # one (set by _planning_worker / PUT /api/runs/{id}/plan).
            stored = ctx.store.get_plan(run_id)
            if stored is None or not stored["tasks"]:
                raise SlopChurnerError("no approved plan for this run")
            plan = planner.Plan(
                test_cmd=stored["test_cmd"],
                tasks=[planner.Task(id=int(t["task_id"]), title=t["title"],
                                    description=t["description"])
                       for t in stored["tasks"]],
            )
        report = engine.run(
            cfg,
            params.get("prompt", ""),
            # A fresh run executes the pre-supplied plan; a retry reuses
            # the existing plan and runs a single task.
            only=only_task,
            plan=plan,
            sink=sink,
            is_cancelled=cancel.is_set,
        )
        if cancel.is_set():
            _close("cancelled", None)
        elif report.done:
            # Work is staged on slop-churner/run-<id>; park until the user accepts
            # or rejects it. The repo claim stays held until then.
            _close("awaiting_acceptance", None)
        else:
            _close("failed", None)
    except (SlopChurnerError, ValueError, OSError) as exc:
        ctx.hub.publish(run_id, {"name": "error", "data": {"message": str(exc)}})
        _close("error", str(exc))
    except Exception as exc:  # unexpected
        ctx.hub.publish(run_id, {"name": "error", "data": {"message": f"internal error: {exc}"}})
        _close("error", f"internal error: {exc}")
    finally:
        ctx.workers.pop(run_id, None)
        row = ctx.store.get_run(run_id)
        if row is None or row["status"] != "awaiting_acceptance":
            ctx.release_repo(params.get("repo") or ".", run_id)


class ServerContext:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.store = Store(db_path)
        self.hub = Hub()
        self.workers: dict[int, threading.Event] = {}
        # resolved repo path -> active run id (one job per repo at a time)
        self.repo_runs: dict[str, int] = {}
        self._claim_lock = threading.Lock()

    def claim_repo(self, repo: str, run_id: int) -> int | None:
        """Reserve a repo for a new run. Returns the conflicting run id, if any.

        Only conflicts with runs that are *still active*; a finished run whose
        cleanup hasn't released its claim yet must not block us.
        """
        key = str(Path(repo).resolve())
        with self._claim_lock:
            cur = self.repo_runs.get(key)
            if cur is not None and cur in self.workers:
                return cur
            self.repo_runs[key] = run_id
            return None

    def release_repo(self, repo: str, run_id: int) -> None:
        key = str(Path(repo).resolve())
        with self._claim_lock:
            if self.repo_runs.get(key) == run_id:
                del self.repo_runs[key]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    ctx: ServerContext  # injected by serve()
    server_version = f"slop-churner/{__version__}"

    # -- helpers ------------------------------------------------------------

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode())
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        sys.stderr.write("slop-churner web: " + fmt % args + "\n")

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._html(200, _INDEX_HTML)
            elif path == "/api/health":
                self._json(200, {"ok": True, "version": __version__,
                                 "db": str(self.ctx.store.path)})
            elif path == "/api/runs":
                runs = []
                for r in self.ctx.store.list_runs():
                    d = dict(r)
                    d["params"] = json.loads(d["params"])
                    d["running"] = r["id"] in self.ctx.workers
                    runs.append(d)
                self._json(200, {"runs": runs})
            elif m := re.fullmatch(r"/api/runs/(\d+)", path):
                self._get_run(int(m.group(1)))
            elif m := re.fullmatch(r"/api/runs/(\d+)/tasks/(\d+)", path):
                self._get_task(int(m.group(1)), int(m.group(2)))
            elif m := re.fullmatch(r"/api/runs/(\d+)/diff", path):
                self._diff(int(m.group(1)))
            elif m := re.fullmatch(r"/api/runs/(\d+)/events", path):
                self._sse(int(m.group(1)))
            else:
                self._json(404, {"error": "not found"})
        except BrokenPipeError:
            pass

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/runs":
                self._create_run()
            elif m := re.fullmatch(r"/api/runs/(\d+)/cancel", path):
                self._cancel(int(m.group(1)))
            elif m := re.fullmatch(r"/api/runs/(\d+)/approve", path):
                self._approve(int(m.group(1)))
            elif m := re.fullmatch(r"/api/runs/(\d+)/reject", path):
                self._reject(int(m.group(1)))
            elif m := re.fullmatch(r"/api/runs/(\d+)/accept", path):
                self._accept_run(int(m.group(1)))
            elif m := re.fullmatch(r"/api/runs/(\d+)/discard", path):
                self._discard_run(int(m.group(1)))
            elif m := re.fullmatch(r"/api/runs/(\d+)/retry/(\d+)", path):
                self._retry(int(m.group(1)), int(m.group(2)))
            else:
                self._json(404, {"error": "not found"})
        except BrokenPipeError:
            pass

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if m := re.fullmatch(r"/api/runs/(\d+)/plan", path):
                self._update_plan(int(m.group(1)))
            else:
                self._json(404, {"error": "not found"})
        except BrokenPipeError:
            pass

    # -- endpoints ------------------------------------------------------------

    def _create_run(self) -> None:
        b = self._body()
        prompt = (b.get("prompt") or "").strip()
        if not prompt:
            self._json(400, {"error": "prompt is required"})
            return
        repo = (b.get("repo") or ".").strip()
        try:
            run_id = self.ctx.store.create_run(repo, prompt, {k: b.get(k) for k in (
                "repo", "base", "agent", "model", "max_iterations", "max_tasks",
                "budget", "test_cmd", "agent_timeout", "test_timeout",
            )})
        except SlopChurnerError as exc:
            self._json(400, {"error": str(exc)})
            return
        # Validate config early (repo must be a git repo etc.) without running.
        try:
            _build_cfg({**(b or {}), "repo": repo})
        except SlopChurnerError as exc:
            self.ctx.store.finish_run(run_id, "error", str(exc))
            self._json(400, {"error": str(exc), "run_id": run_id})
            return
        # One active job per repo: the state file and git repo are shared.
        conflict = self.ctx.claim_repo(repo, run_id)
        if conflict is not None:
            self.ctx.store.finish_run(run_id, "error", f"repo already in use by run {conflict}")
            self._json(409, {"error": f"a run for this repo is already active (#{conflict})",
                             "run_id": run_id})
            return
        # Planning-only stage: the run parks in 'awaiting_approval' until an
        # explicit POST /api/runs/{id}/approve starts execution.
        t = threading.Thread(target=_planning_worker, args=(self.ctx, run_id, b),
                             name=f"slop-churner-plan-{run_id}", daemon=True)
        t.start()
        self._json(201, {"run_id": run_id})

    def _update_plan(self, run_id: int) -> None:
        row = self.ctx.store.get_run(run_id)
        if row is None:
            self._json(404, {"error": "no such run"})
            return
        if row["status"] != "awaiting_approval":
            self._json(409, {"error": "the plan can only be edited while the run is awaiting approval"})
            return
        b = self._body()
        raw = b.get("tasks")
        if not isinstance(raw, list) or not raw:
            self._json(400, {"error": "'tasks' must be a non-empty list"})
            return
        tasks: list[dict[str, Any]] = []
        for i, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                self._json(400, {"error": f"task #{i} is not an object"})
                return
            try:
                task_id = int(item.get("task_id"))
            except (TypeError, ValueError):
                self._json(400, {"error": f"task #{i} needs an integer task_id"})
                return
            title = str(item.get("title") or "").strip()
            description = str(item.get("description") or "").strip()
            if not title or not description:
                self._json(400, {"error": f"task #{i} is missing title or description"})
                return
            tasks.append({"task_id": task_id, "title": title, "description": description})
        test_cmd = b.get("test_cmd")
        if test_cmd is not None and not isinstance(test_cmd, str):
            self._json(400, {"error": "test_cmd must be a string or null"})
            return
        self.ctx.store.replace_tasks(run_id, tasks)
        self.ctx.store.set_plan_test_cmd(run_id, test_cmd or None)
        plan = self.ctx.store.get_plan(run_id)
        # Notify open UIs so the task list refreshes (same shape as the
        # engine's 'planned' event).
        self.ctx.hub.publish(run_id, {"name": "planned", "data": {
            "tasks": [{"id": t["task_id"], "title": t["title"],
                       "description": t["description"], "status": t["status"],
                       "iterations": t["iterations"]}
                      for t in plan["tasks"]],
            "test_cmd": plan["test_cmd"],
        }})
        self._json(200, {"ok": True, "plan": plan})

    def _approve(self, run_id: int) -> None:
        row = self.ctx.store.get_run(run_id)
        if row is None:
            self._json(404, {"error": "no such run"})
            return
        if row["status"] != "awaiting_approval":
            self._json(409, {"error": "run is not awaiting approval"})
            return
        params = {**row["params"], "prompt": row["prompt"]}
        # Re-claim the repo for execution (the planning worker released it).
        conflict = self.ctx.claim_repo(params.get("repo") or ".", run_id)
        if conflict is not None and conflict != run_id:
            self._json(409, {"error": f"repo already in use by run {conflict}"})
            return
        self.ctx.store.set_run_status(run_id, "running")
        t = threading.Thread(target=_worker, args=(self.ctx, run_id, params),
                             name=f"slop-churner-run-{run_id}", daemon=True)
        t.start()
        self._json(200, {"ok": True})

    def _reject(self, run_id: int) -> None:
        row = self.ctx.store.get_run(run_id)
        if row is None:
            self._json(404, {"error": "no such run"})
            return
        if row["status"] != "awaiting_approval":
            self._json(409, {"error": "run is not awaiting approval"})
            return
        # Close without executing anything; the repo claim was already
        # released when the planning worker finished.
        self.ctx.store.finish_run(run_id, "cancelled", None)
        self.ctx.hub.publish(run_id, {"name": "closed", "data": {"status": "cancelled", "error": None}})
        self._json(200, {"ok": True})

    def _get_run(self, run_id: int) -> None:
        d = self.ctx.store.get_run(run_id)
        if d is None:
            self._json(404, {"error": "no such run"})
            return
        d["running"] = run_id in self.ctx.workers
        d["tasks"] = self.ctx.store.get_tasks(run_id)
        self._json(200, d)

    def _get_task(self, run_id: int, task_id: int) -> None:
        if self.ctx.store.get_run(run_id) is None:
            self._json(404, {"error": "no such run"})
            return
        tasks = self.ctx.store.get_tasks(run_id)
        task = next((t for t in tasks if t["task_id"] == task_id), None)
        if task is None:
            self._json(404, {"error": "no such task"})
            return
        steps = self.ctx.store.get_steps(run_id, task_id)
        self._json(200, {"task": task, "steps": steps, "running": run_id in self.ctx.workers})

    def _diff(self, run_id: int) -> None:
        row = self.ctx.store.get_run(run_id)
        if row is None:
            self._json(404, {"error": "no such run"})
            return
        params = {**row["params"], "prompt": row["prompt"]}
        try:
            cfg = _build_cfg(params)
        except SlopChurnerError as exc:
            self._json(400, {"error": str(exc)})
            return
        branch = gitutil.run_branch_name(run_id)
        if not gitutil.branch_exists(cfg.repo, branch):
            self._json(404, {"error": f"staging branch {branch} not found "
                                     "(run may not have staged any work)"})
            return
        d = gitutil.diff_overview(cfg.repo, cfg.base_branch, branch)
        d["run_branch"] = branch
        d["base_branch"] = cfg.base_branch
        d["tasks"] = self.ctx.store.get_tasks(run_id)
        self._json(200, d)

    def _accept_run(self, run_id: int) -> None:
        row = self.ctx.store.get_run(run_id)
        if row is None:
            self._json(404, {"error": "no such run"})
            return
        if row["status"] != "awaiting_acceptance":
            self._json(409, {"error": "run is not awaiting acceptance "
                                     f"(status: {row['status']})"})
            return
        repo = row["repo"]
        # Another active run may have taken the repo in the meantime.
        conflict = self.ctx.claim_repo(repo, run_id)
        if conflict is not None and conflict != run_id:
            self._json(409, {"error": f"repo already in use by run {conflict}"})
            return
        params = {**row["params"], "prompt": row["prompt"]}
        try:
            cfg = _build_cfg(params)
            cfg.run_branch = gitutil.run_branch_name(run_id)
            state = State.load(cfg.state_path)
            if state is None or not state.run_branch:
                state = State(prompt=row["prompt"], base_branch=cfg.base_branch,
                              test_cmd=None, run_branch=cfg.run_branch)
            else:
                state.run_branch = cfg.run_branch
            engine.accept_run(cfg, state)
        except (SlopChurnerError, OSError) as exc:
            self._json(409, {"error": f"accept failed: {exc}"})
            return
        self.ctx.store.finish_run(run_id, "accepted", None)
        self.ctx.release_repo(repo, run_id)
        self.ctx.hub.publish(run_id, {"name": "closed", "data": {"status": "accepted", "error": None}})
        self._json(200, {"ok": True, "status": "accepted"})

    def _discard_run(self, run_id: int) -> None:
        """Reject the finished run: discard its staging branch + worktrees."""
        row = self.ctx.store.get_run(run_id)
        if row is None:
            self._json(404, {"error": "no such run"})
            return
        if row["status"] != "awaiting_acceptance":
            self._json(409, {"error": "run is not awaiting acceptance "
                                     f"(status: {row['status']})"})
            return
        repo = row["repo"]
        conflict = self.ctx.claim_repo(repo, run_id)
        if conflict is not None and conflict != run_id:
            self._json(409, {"error": f"repo already in use by run {conflict}"})
            return
        params = {**row["params"], "prompt": row["prompt"]}
        try:
            cfg = _build_cfg(params)
            cfg.run_branch = gitutil.run_branch_name(run_id)
            state = State.load(cfg.state_path)
            if state is None or not state.run_branch:
                state = State(prompt=row["prompt"], base_branch=cfg.base_branch,
                              test_cmd=None, run_branch=cfg.run_branch)
            else:
                state.run_branch = cfg.run_branch
            engine.reject_run(cfg, state)
        except (SlopChurnerError, OSError) as exc:
            self._json(409, {"error": f"discard failed: {exc}"})
            return
        self.ctx.store.finish_run(run_id, "rejected", None)
        self.ctx.release_repo(repo, run_id)
        self.ctx.hub.publish(run_id, {"name": "closed", "data": {"status": "rejected", "error": None}})
        self._json(200, {"ok": True, "status": "rejected"})

    def _cancel(self, run_id: int) -> None:
        cancel = self.ctx.workers.get(run_id)
        if cancel is None:
            self._json(409, {"error": "run is not active"})
            return
        cancel.set()
        self._json(200, {"ok": True})

    def _retry(self, run_id: int, task_id: int) -> None:
        row = self.ctx.store.get_run(run_id)
        if row is None:
            self._json(404, {"error": "no such run"})
            return
        if run_id in self.ctx.workers:
            self._json(409, {"error": "run is still active; cancel it first"})
            return
        if row["status"] == "awaiting_approval":
            self._json(409, {"error": "approve or reject the plan before retrying"})
            return
        if row["status"] == "awaiting_acceptance":
            self._json(409, {"error": "accept or reject the staged run before retrying"})
            return
        task = next((t for t in self.ctx.store.get_tasks(run_id) if t["task_id"] == task_id), None)
        if task is None:
            self._json(404, {"error": "no such task"})
            return
        if task["status"] == "done":
            self._json(409, {"error": "task is already done"})
            return
        # Reuse the original parameters as the prompt/config source.
        params = {**row["params"], "prompt": row["prompt"]}
        conflict = self.ctx.claim_repo(params.get("repo") or ".", run_id)
        if conflict is not None and conflict != run_id:
            self._json(409, {"error": f"repo already in use by run {conflict}"})
            return
        self.ctx.store.set_run_status(run_id, "running")
        # Reset the task row so progress shows fresh.
        self.ctx.store.upsert_task(run_id, {**task, "status": "pending", "iterations": 0})
        t = threading.Thread(target=_worker, args=(self.ctx, run_id, params, task_id),
                             name=f"slop-churner-retry-{run_id}-{task_id}", daemon=True)
        t.start()
        self._json(200, {"ok": True})

    def _sse(self, run_id: int) -> None:
        if self.ctx.store.get_run(run_id) is None:
            self._json(404, {"error": "no such run"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.ctx.hub.subscribe(run_id)
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.ctx.hub.unsubscribe(run_id, q)


def _load_index_html() -> str:
    p = Path(__file__).resolve().parent / "ui.html"
    if p.exists():
        return p.read_text()
    return "<h1>slop-churner</h1><p>UI asset 'ui.html' not found next to slop-churner/web.py</p>"


_INDEX_HTML = _load_index_html()


# ---------------------------------------------------------------------------
# Cleartext HTTP/2 (h2c) on the same port
#
# Every connection is sniffed on its first bytes: the h2 client connection
# preface (RFC 7540 "prior knowledge") drives an h2.connection.H2Connection,
# anything else falls back to the stdlib HTTP/1.1 Handler unchanged.
# Handler's routing (do_GET/do_POST/do_PUT) is reused as-is; only the I/O
# primitives differ.
# ---------------------------------------------------------------------------

if _H2:

    class _HeadReader:
        """rfile for the HTTP/1.1 fallback path: replays the leading bytes
        we already inspected for the h2 preface, then proxies to the socket.
        """

        def __init__(self, head: bytes, underlying: Any) -> None:
            self._head = head
            self._u = underlying

        def readline(self, n: int = -1) -> bytes:
            buf = self._head
            self._head = b""
            if b"\n" not in buf:
                buf += self._u.readline()
                if b"\n" not in buf:  # incomplete request line / EOF
                    self._head = buf
                    return b""
            line, rest = buf.split(b"\n", 1)
            if rest:
                self._head = rest + b"\n"
            return line + b"\n"

        def read(self, n: int = -1) -> bytes:
            if self._head:
                data, self._head = self._head, b""
                if n == -1:
                    return data + self._u.read()
                if len(data) >= n:
                    self._head = data[n:]
                    return data[:n]
                return data + self._u.read(n - len(data))
            return self._u.read(n)

    class _H2Handler(Handler):
        """One h2 request/stream, reusing Handler's endpoint methods.

        Same routing as the HTTP/1.1 Handler; only the request source and
        the response sink differ (response frames are written by the
        _H2Session after the endpoint method returns).
        """

        def __init__(self, session: "_H2Session", stream_id: int) -> None:
            self.session = session
            self.stream_id = stream_id
            self.path = "/"
            self.headers: dict[str, str] = {}
            self._request_body = b""
            # Defaults: an endpoint that blew up still gets a JSON 500.
            self._status = 500
            self._resp_headers: list[tuple[str, str]] = [
                ("content-type", "application/json")]
            self._chunks: list[bytes] = []
            self._streaming = False  # set by _sse(): stream handled out-of-band

        # -- response primitives (h2 replacements of send_response & co) ----

        def _emit(self, code: int, content_type: str, body: bytes) -> None:
            self._status = code
            self._resp_headers = [("content-type", content_type)]
            self._chunks = [body] if body else []

        def _json(self, code: int, payload: Any) -> None:
            self._emit(code, "application/json",
                       json.dumps(payload, default=str).encode())

        def _html(self, code: int, text: str) -> None:
            self._emit(code, "text/html; charset=utf-8", text.encode())

        def _body(self) -> dict[str, Any]:
            if not self._request_body:
                return {}
            try:
                data = json.loads(self._request_body.decode())
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}

        def _sse(self, run_id: int) -> None:
            if self.session.ctx.store.get_run(run_id) is None:
                self._json(404, {"error": "no such run"})
                return
            self._streaming = True
            q = self.session.ctx.hub.subscribe(run_id)
            try:
                self.session.send_head(self.stream_id, 200, {
                    "content-type": "text/event-stream",
                    "cache-control": "no-cache",
                })
                while True:
                    try:
                        ev = q.get(timeout=15)
                        chunk = f"data: {json.dumps(ev)}\n\n".encode()
                    except queue.Empty:
                        chunk = b": ping\n\n"
                    self.session.send_data(self.stream_id, chunk)
                    self.session.flush()  # deliver each event as it arrives
            except Exception:  # client gone / stream reset: stop the stream
                pass
            finally:
                self.session.ctx.hub.unsubscribe(run_id, q)

    class _H2Session:
        """One h2c connection: h2.connection.H2Connection on a raw socket.

        Handles SETTINGS, HEADERS, DATA, RST_STREAM and WINDOW_UPDATE;
        requests are mapped onto Handler's routing and responses are sent
        as HEADERS + DATA frames.
        """

        def __init__(self, sock: socket.socket, head: bytes,
                     ctx: ServerContext) -> None:
            self.sock = sock
            self.ctx = ctx
            self.conn = h2.connection.H2Connection(
                h2.config.H2Configuration(client_side=False,
                                          header_encoding="utf-8"))
            self.conn.initiate_connection()
            self._recv_buf = head
            self._heads: dict[int, list[tuple[str, str]]] = {}
            self._bodies: dict[int, list[bytes]] = {}

        # -- low-level io ----------------------------------------------------

        def _recv(self, n: int) -> bytes:
            data = self._recv_buf
            self._recv_buf = b""
            if len(data) < n:
                data += self.sock.recv(n - len(data))
            if not data:
                raise ConnectionError("client closed the connection")
            return data

        def flush(self) -> None:
            out = self.conn.data_to_send()
            if out:
                self.sock.sendall(out)

        # -- stream writers ----------------------------------------------------

        def send_head(self, stream_id: int, status: int,
                      headers: dict[str, str]) -> None:
            self.conn.send_headers(
                stream_id,
                [(":status", str(status))] + list(headers.items()),
                end_stream=False)
            self.flush()

        def send_data(self, stream_id: int, data: bytes) -> None:
            self.conn.send_data(stream_id, data, end_stream=False)
            self.flush()

        def _send_response(self, stream_id: int, h: _H2Handler) -> None:
            self.conn.send_headers(
                stream_id,
                [(":status", str(h._status))] + h._resp_headers,
                end_stream=not h._chunks)
            self.flush()
            for i, chunk in enumerate(h._chunks):
                self.conn.send_data(
                    stream_id, chunk,
                    end_stream=(i == len(h._chunks) - 1))
                self.flush()

        # -- event handling ----------------------------------------------------

        def _dispatch(self, stream_id: int) -> None:
            headers = self._heads.pop(stream_id, [])
            body = b"".join(self._bodies.pop(stream_id, ()))
            pseudo = {k: v for k, v in headers if k.startswith(":")}
            fields = {k: v for k, v in headers if not k.startswith(":")}
            h = _H2Handler(self, stream_id)
            h.path = pseudo.get(":path", "/")
            h.headers = fields
            h._request_body = body
            method = pseudo.get(":method", "GET").upper()
            try:
                if method in ("GET", "HEAD"):
                    h.do_GET()
                elif method == "POST":
                    h.do_POST()
                elif method == "PUT":
                    h.do_PUT()
                else:
                    h._json(405, {"error": "method not allowed"})
            except Exception as exc:  # endpoint blew up: still answer
                print(f"slop-churner web: h2 stream {stream_id} error: {exc}",
                      file=sys.stderr)
            if h._streaming:
                return  # SSE already handled the stream out-of-band
            try:
                self._send_response(stream_id, h)
            except H2Error as exc:  # peer reset the stream: drop it
                print(f"slop-churner web: h2 stream {stream_id} dropped: {exc}",
                      file=sys.stderr)

        def run(self) -> None:
            try:
                while True:
                    events = self.conn.receive_data(self._recv(65536))
                    self.flush()
                    for ev in events:
                        if isinstance(ev, h2.events.StreamReset):
                            self._heads.pop(ev.stream_id, None)
                            self._bodies.pop(ev.stream_id, None)
                        elif isinstance(ev, h2.events.WindowUpdated):
                            # A received WINDOW_UPDATE grows our send window;
                            # h2 already applied it, nothing to answer.
                            # (Returning window is done via
                            # acknowledge_received_data after consuming DATA.)
                            pass
                        elif isinstance(ev, h2.events.DataReceived):
                            self.conn.acknowledge_received_data(
                                len(ev.data), ev.stream_id)
                            self._bodies.setdefault(ev.stream_id, []).append(
                                ev.data)
                            if ev.stream_ended:
                                self._dispatch(ev.stream_id)
                        elif isinstance(ev, _H2_REQ_EVENT):
                            self._heads[ev.stream_id] = list(ev.headers)
                            if ev.stream_ended:
                                self._dispatch(ev.stream_id)
                        # SettingsAcknowledged / PingReceived / etc.: nothing to do
            except ConnectionError:
                return  # peer closed the connection
            except H2Error as exc:
                print(f"slop-churner web: h2 protocol error: {exc}", file=sys.stderr)
                try:  # signal the peer, then drop the connection
                    self.conn.close_connection()
                    self.flush()
                except Exception:
                    pass

    class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    class _DualRequestHandler:
        """Per-connection handler for _ThreadingTCPServer.

        Sniffs the first bytes: the h2 client preface (prior knowledge)
        means cleartext HTTP/2, anything else falls back to the stdlib
        HTTP/1.1 Handler unchanged.
        """

        def __init__(self, sock: socket.socket, addr: tuple,
                     server: Any) -> None:
            self.sock = sock
            self.addr = addr
            self.server = server
            try:
                head = b""
                closed = False
                while len(head) < len(_H2_PREFACE):
                    chunk = sock.recv(len(_H2_PREFACE) - len(head))
                    if not chunk:
                        closed = True
                        break
                    head += chunk
                if _H2 and (head == _H2_PREFACE
                            or (closed and _H2_PREFACE.startswith(head))):
                    _H2Session(sock, head, Handler.ctx).run()
                else:
                    self._http11(head)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

        def _http11(self, head: bytes) -> None:
            # Bypass BaseHTTPRequestHandler.__init__/setup and wire the
            # already-inspected leading bytes back into rfile.
            h = Handler.__new__(Handler)
            h.server = self.server
            h.client_address = self.addr
            h.connection = self.sock
            h.request = self.sock
            h.rfile = _HeadReader(head, self.sock.makefile("rb"))
            h.wfile = self.sock.makefile("wb")
            try:
                h.handle()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception as exc:
                h.log_message("error while processing request: %s", exc)
            finally:
                try:
                    h.wfile.close()
                except (OSError, ValueError):
                    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve(host: str = "127.0.0.1", port: int = 8787,
          db: Path | str | None = None, open_browser: bool = False,
          http2: bool = True) -> int:
    ctx = ServerContext(db or default_db_path())
    Handler.ctx = ctx
    if http2 and _H2:
        httpd: Any = _ThreadingTCPServer((host, port), _DualRequestHandler)
        proto = "HTTP/2 (h2c) + HTTP/1.1"
    else:
        if http2:
            print("slop-churner web: 'h2' is not installed; serving HTTP/1.1 only",
                  file=sys.stderr)
        httpd = ThreadingHTTPServer((host, port), Handler)
        proto = "HTTP/1.1"
    url = f"http://{host}:{port}/"
    print(f"slop-churner web: serving {url}  [{proto}]  (db: {ctx.store.path})")
    print("slop-churner web: press Ctrl-C to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nslop-churner web: shutting down")
    finally:
        httpd.server_close()
        ctx.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
