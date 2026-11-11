"""Event-sink plumbing shared by engine, runner and the web layer.

A sink is a plain callable receiving ``(event_name, data)`` pairs:

- ``run_started``  {repo, prompt, base_branch, test_cmd}
- ``planned``      {tasks: [task_dict...], test_cmd}
- ``task_updated`` {id, title, description, status, iterations, ...}
- ``step``         {task_id, iteration, stage, ok, output, duration}
- ``run_finished`` {status, done, failed, skipped, duration}

Pass ``None`` where a sink is accepted to disable observation (the CLI).
"""
from __future__ import annotations

from typing import Any, Callable

Sink = Callable[[str, dict[str, Any]], None]


def noop(*_args: Any) -> None:
    """Default sink: drop every event."""
