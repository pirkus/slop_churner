from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import prompts
from .agent import run_agent
from .config import Config
from .errors import RalphError
from .state import State, Task


@dataclass
class Plan:
    test_cmd: str | None
    tasks: list[Task]


def _extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of an agent's reply."""
    text = text.strip()
    # Strip markdown fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RalphError(f"planner returned no JSON object:\n{text[:500]}")
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RalphError(f"planner returned invalid JSON: {exc}\n{candidate[:500]}") from exc


def _validate(obj: dict, max_tasks: int) -> Plan:
    raw_tasks = obj.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise RalphError("planner JSON must contain a non-empty 'tasks' array")
    if len(raw_tasks) > max_tasks:
        raise RalphError(f"planner returned {len(raw_tasks)} tasks (max {max_tasks})")

    tasks: list[Task] = []
    for i, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            raise RalphError(f"task #{i} is not an object")
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        if not title or not description:
            raise RalphError(f"task #{i} is missing title or description")
        tasks.append(Task(id=i, title=title, description=description))

    test_cmd = obj.get("test_cmd")
    if test_cmd is not None and not isinstance(test_cmd, str):
        test_cmd = None
    return Plan(test_cmd=test_cmd or None, tasks=tasks)


def _build_planner_prompt(cfg: Config, request: str) -> str:
    return (
        f"{prompts.plan_system(cfg)}\n"
        "The request to decompose:\n"
        f"{request}\n\n"
        "First inspect the repository (list files, read key files, find the test runner), "
        "then respond with the JSON object only.\n"
    )


def plan(cfg: Config, request: str) -> Plan:
    """Run the planner agent once and return a validated task list."""
    prompt = _build_planner_prompt(cfg, request)
    result = run_agent(
        cfg,
        prompt,
        cwd=cfg.repo,
        timeout=cfg.agent_timeout,
        label="planning tasks",
    )
    # Hard infra failures (missing binary, timeout) are not retryable here.
    if result.returncode in (127, -1) or (not result.ok and not result.output.strip()):
        raise RalphError(
            f"planner agent could not run (rc={result.returncode}): "
            f"{result.output.strip() or 'no output'}"
        )
    plan = _validate(_extract_json(result.output), cfg.max_tasks)
    return plan


def build_state(cfg: Config, request: str, plan: Plan) -> State:
    test_cmd = cfg.test_cmd or plan.test_cmd
    return State(
        prompt=request,
        base_branch=cfg.base_branch,
        test_cmd=test_cmd,
        tasks=plan.tasks,
    )
