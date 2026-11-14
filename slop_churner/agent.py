from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable
from pathlib import Path

from .config import Config


@dataclass
class AgentResult:
    ok: bool
    output: str
    returncode: int
    duration: float
    # Diagnostics when the run did not complete cleanly (stream error,
    # abort, stderr). ``output`` stays clean so callers can still parse it.
    error: str | None = None


@contextlib.contextmanager
def heartbeat(label: str, interval: float = 15.0, enabled: bool = True):
    """Print a one-line elapsed-time ticker while a long step is running."""
    if not enabled:
        yield
        return
    stop = threading.Event()
    t0 = time.time()

    def tick() -> None:
        while not stop.wait(interval):
            print(f"    … {label} ({time.time() - t0:.0f}s)", flush=True)

    th = threading.Thread(target=tick, daemon=True)
    th.start()
    try:
        yield
    finally:
        stop.set()
        th.join(timeout=0)


def _build_cmd(cfg: Config, prompt: str, tools: str | None = None, json_mode: bool = False) -> list[str]:
    cmd = [cfg.agent_cmd]
    if json_mode:
        cmd += ["--mode", "json"]
    cmd += ["-p", prompt]
    if cfg.model:
        cmd += ["--model", cfg.model]
    if tools:
        cmd += ["--tools", tools]
    return cmd


def _env() -> dict[str, str]:
    # Non-interactive: make sure pi never waits on a TTY prompt.
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    return env


def _assistant_text(content) -> str | None:
    """Extract an assistant message's text from its content (str or block list)."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        joined = "".join(parts)
        return joined if joined else None
    return None


def _consume_json_stream(proc, on_output: Callable[[str], None]):
    """Read a ``pi --mode json`` JSONL stream.

    Emits live progress to ``on_output`` as events arrive and returns
    ``(final_text, aborted, saw_error, final_stop_reason)`` following the
    reconstruction rules in ``docs/json.md``:

    * ``message_update`` records are delta-only; the authoritative text is
      assembled from ``text_delta`` deltas (emitted live here) and the final
      answer is taken from the last assistant ``message_end``.
    * ``agent_settled.aborted`` flags a run that was aborted.
    * a provider ``error`` event flags a failed stream.
    * a run can exit 0 even when it failed, so success is judged from these
      events (and the ``stopReason``), not just the return code.
    """
    final_text: str | None = None
    aborted = False
    saw_error = False
    final_stop: str | None = None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "message_update":
            a = ev.get("assistantMessageEvent") or {}
            at = a.get("type")
            if at == "thinking_start":
                on_output("\n\u273b thinking\u2026\n")
            elif at == "thinking_delta":
                d = a.get("delta", "")
                if d:
                    on_output(d)
            elif at == "text_start":
                on_output("\n\u270e response\n")
            elif at == "text_delta":
                d = a.get("delta", "")
                if d:
                    on_output(d)
            elif at == "error":
                saw_error = True
                on_output(f"\n\u2717 {a.get('error') or 'agent stream error'}\n")
        elif etype == "tool_execution_start":
            name = ev.get("toolName", "tool")
            args = ev.get("args") or {}
            hint = args.get("command") or args.get("path") or args.get("file_path") or ""
            on_output(f"\n$ {name}" + (f" {hint}" if hint else "") + "\n")
        elif etype == "message_end":
            m = ev.get("message") or {}
            if m.get("role") == "assistant":
                txt = _assistant_text(m.get("content"))
                if txt is not None:
                    final_text = txt
                sr = m.get("stopReason")
                if sr:
                    final_stop = sr
        elif etype == "agent_settled":
            aborted = bool(ev.get("aborted", False))
    return final_text, aborted, saw_error, final_stop


def run_agent(
    cfg: Config,
    prompt: str,
    *,
    cwd: Path,
    tools: str | None = None,
    timeout: int | None = None,
    label: str = "agent",
    quiet: bool = False,
    on_output: Callable[[str], None] | None = None,
) -> AgentResult:
    """Run a single fresh-context agent invocation and return its result.

    When a live ``on_output`` callback is supplied and the agent is ``pi``,
    the run uses ``pi --mode json`` so progress (thinking, tool calls, and
    the streamed answer) is delivered as it happens, instead of only at the
    end. See ``docs/cli-integration.md`` (JSON mode) and ``docs/json.md``
    (event stream + reconstruction rules) in the pi package.

    Without a callback (e.g. the CLI) or for non-pi agents, the plain
    print-mode stdout is streamed line-by-line to ``on_output``.
    """
    json_mode = on_output is not None and os.path.basename(cfg.agent_cmd) == "pi"
    cmd = _build_cmd(cfg, prompt, tools, json_mode=json_mode)
    cwd.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    effective_timeout = timeout if timeout is not None else cfg.agent_timeout
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return AgentResult(
            ok=False,
            output=f"agent command not found: {cfg.agent_cmd}",
            returncode=127,
            duration=time.time() - t0,
        )

    timed_out = threading.Event()

    def _watchdog() -> None:
        if effective_timeout is None or timed_out.wait(effective_timeout):
            return
        timed_out.set()
        proc.kill()

    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            stderr_chunks.append(line)

    wd = threading.Thread(target=_watchdog, daemon=True)
    wd.start()
    err = threading.Thread(target=_drain_stderr, daemon=True)
    err.start()
    stdout_lines: list[str] = []
    final_text: str | None = None
    aborted = False
    saw_error = False
    final_stop: str | None = None
    try:
        with heartbeat(label, enabled=not quiet):
            if json_mode:
                final_text, aborted, saw_error, final_stop = _consume_json_stream(
                    proc, on_output
                )
            else:
                for line in proc.stdout:
                    stdout_lines.append(line)
                    if on_output is not None:
                        on_output(line)
            err.join()
            proc.wait()
    finally:
        err.join()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        wd.join(timeout=0)

    duration = time.time() - t0
    stderr = "".join(stderr_chunks)

    if timed_out.is_set():
        partial = (final_text or "") if json_mode else "".join(stdout_lines)
        return AgentResult(
            ok=False,
            output=f"agent timed out after {effective_timeout}s\n{(partial or '').strip()}".strip(),
            error=stderr.strip() or None,
            returncode=-1,
            duration=duration,
        )

    if json_mode:
        out = (final_text or "").strip()
        ok = (
            proc.returncode == 0
            and not aborted
            and not saw_error
            and final_stop not in ("error", "aborted", "length")
        )
        if not ok:
            reason = []
            if aborted:
                reason.append("aborted")
            if saw_error:
                reason.append("stream error")
            if final_stop in ("error", "aborted", "length"):
                reason.append(f"stopReason={final_stop}")
            if proc.returncode != 0:
                reason.append(f"rc={proc.returncode}")
            diag = "; ".join(reason) if reason else "failed"
            if stderr.strip():
                diag += f"\n[stderr]\n{stderr.strip()}"
            return AgentResult(
                ok=False, output=out, error=diag,
                returncode=proc.returncode, duration=duration,
            )
        return AgentResult(
            ok=True, output=out, returncode=proc.returncode, duration=duration,
        )

    out = "".join(stdout_lines)
    if stderr:
        out += "\n[stderr]\n" + stderr
    return AgentResult(
        ok=proc.returncode == 0,
        output=out.strip(),
        returncode=proc.returncode,
        duration=duration,
    )
