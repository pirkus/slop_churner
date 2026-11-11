from __future__ import annotations

import contextlib
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


def _build_cmd(cfg: Config, prompt: str, tools: str | None = None) -> list[str]:
    cmd = [cfg.agent_cmd, "-p", prompt]
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
    """Run a single fresh-context pi invocation and return its result.

    Stdout is streamed line-by-line: each line is passed to ``on_output``
    as it arrives (if a callback was given), instead of only being
    visible once the call returns.
    """
    cmd = _build_cmd(cfg, prompt, tools)
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
    try:
        with heartbeat(label, enabled=not quiet):
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

    if timed_out.is_set():
        partial = "".join(stdout_lines)
        return AgentResult(
            ok=False,
            output=f"agent timed out after {effective_timeout}s\n{partial}".strip(),
            returncode=-1,
            duration=time.time() - t0,
        )

    out = "".join(stdout_lines)
    if stderr_chunks:
        out += "\n[stderr]\n" + "".join(stderr_chunks)
    return AgentResult(
        ok=proc.returncode == 0,
        output=out.strip(),
        returncode=proc.returncode,
        duration=time.time() - t0,
    )
