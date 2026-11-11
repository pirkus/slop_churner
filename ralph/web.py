"""Web UI + JSON API for ralph.

Serves a single-page dashboard where you can:
  - create a run from a prompt + tunable parameters,
  - watch jobs progress live (SSE),
  - browse every step's log (implement/fix, test, pr),
  - cancel a running job or retry a failed task.

Zero third-party dependencies: stdlib http.server + sqlite3.

    ralph web [--host 127.0.0.1] [--port 8787] [--db PATH] [--open]
"""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__, engine, events
from .config import Config
from .errors import RalphError
from .store import Store, default_db_path

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
            print(f"ralph web: store error: {exc}", file=sys.stderr)
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


def _worker(ctx: "ServerContext", run_id: int, params: dict[str, Any],
            only_task: int | None = None) -> None:
    cancel = threading.Event()
    ctx.workers[run_id] = cancel
    sink = WebSink(ctx.store, ctx.hub, run_id)

    def _close(status: str, error: str | None) -> None:
        ctx.store.finish_run(run_id, status, error)
        ctx.hub.publish(run_id, {"name": "closed", "data": {"status": status, "error": error}})

    try:
        ctx.store.set_run_status(run_id, "planning")
        cfg = _build_cfg(params)
        report = engine.run(
            cfg,
            params.get("prompt", ""),
            # A new run always plans fresh for its own prompt; a retry
            # reuses the existing plan and runs a single task.
            force_plan=(only_task is None),
            only=only_task,
            sink=sink,
            is_cancelled=cancel.is_set,
        )
        if cancel.is_set():
            _close("cancelled", None)
        elif report.all_done:
            _close("done", None)
        else:
            _close("failed", None)
    except (RalphError, ValueError, OSError) as exc:
        ctx.hub.publish(run_id, {"name": "error", "data": {"message": str(exc)}})
        _close("error", str(exc))
    except Exception as exc:  # unexpected
        ctx.hub.publish(run_id, {"name": "error", "data": {"message": f"internal error: {exc}"}})
        _close("error", f"internal error: {exc}")
    finally:
        ctx.workers.pop(run_id, None)
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
    server_version = f"ralph/{__version__}"

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
        sys.stderr.write("ralph web: " + fmt % args + "\n")

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
            elif m := re.fullmatch(r"/api/runs/(\d+)/retry/(\d+)", path):
                self._retry(int(m.group(1)), int(m.group(2)))
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
        except RalphError as exc:
            self._json(400, {"error": str(exc)})
            return
        # Validate config early (repo must be a git repo etc.) without running.
        try:
            _build_cfg({**(b or {}), "repo": repo})
        except RalphError as exc:
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
        t = threading.Thread(target=_worker, args=(self.ctx, run_id, b),
                             name=f"ralph-run-{run_id}", daemon=True)
        t.start()
        self._json(201, {"run_id": run_id})

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
                             name=f"ralph-retry-{run_id}-{task_id}", daemon=True)
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
    return "<h1>ralph</h1><p>UI asset 'ui.html' not found next to ralph/web.py</p>"


_INDEX_HTML = _load_index_html()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve(host: str = "127.0.0.1", port: int = 8787,
          db: Path | str | None = None, open_browser: bool = False) -> int:
    ctx = ServerContext(db or default_db_path())
    Handler.ctx = ctx
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"ralph web: serving {url}  (db: {ctx.store.path})")
    print("ralph web: press Ctrl-C to stop")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nralph web: shutting down")
    finally:
        httpd.server_close()
        ctx.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
