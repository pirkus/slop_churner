from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass
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
) -> AgentResult:
    """Run a single fresh-context pi invocation and return its result."""
    cmd = _build_cmd(cfg, prompt, tools)
    cwd.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        with heartbeat(label, enabled=not quiet):
            proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=_env(),
            capture_output=True,
            text=True,
            timeout=timeout if timeout is not None else cfg.agent_timeout,
        )
        out = proc.stdout or ""
        if proc.stderr:
            out += "\n[stderr]\n" + proc.stderr
        return AgentResult(
            ok=proc.returncode == 0,
            output=out.strip(),
            returncode=proc.returncode,
            duration=time.time() - t0,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return AgentResult(
            ok=False,
            output=f"agent timed out after {timeout or cfg.agent_timeout}s\n{partial}".strip(),
            returncode=-1,
            duration=time.time() - t0,
        )
    except FileNotFoundError:
        return AgentResult(
            ok=False,
            output=f"agent command not found: {cfg.agent_cmd}",
            returncode=127,
            duration=time.time() - t0,
        )
