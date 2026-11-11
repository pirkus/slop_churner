from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import events, gitutil, planner, runner
from .config import Config
from .errors import RalphError
from .state import State, Status, Task


@dataclass
class RunReport:
    planned: int
    done: list[Task]
    failed: list[Task]
    skipped: list[Task]
    duration: float

    @property
    def all_done(self) -> bool:
        return not self.failed


def _load_or_plan(cfg: Config, request: str, *, force_plan: bool) -> State:
    existing = State.load(cfg.state_path)
    if existing is not None and not force_plan:
        # Resume: keep the already-planned task list and any progress,
        # but honor the base branch / test cmd from this invocation.
        existing.base_branch = cfg.base_branch
        if cfg.test_cmd:
            existing.test_cmd = cfg.test_cmd
        return existing
    plan = planner.plan(cfg, request)
    state = planner.build_state(cfg, request, plan)
    state.save(cfg.state_path)
    return state


def _budget_left(state: State, cfg: Config) -> bool:
    if cfg.budget is None:
        return True
    used = sum(t.iterations for t in state.tasks)
    return used < cfg.budget


def run(
    cfg: Config,
    request: str,
    *,
    force_plan: bool = False,
    dry_run: bool = False,
    only: int | None = None,
    sink: events.Sink | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> RunReport:
    emit = sink or events.noop
    cancelled = is_cancelled or (lambda: False)
    t0 = time.time()
    gitutil.ensure_excluded(cfg.repo)
    needs_plan = force_plan or State.load(cfg.state_path) is None
    if needs_plan:
        print(f"ralph: planning tasks with '{cfg.agent_cmd}'… "
              f"(first line below appears when it returns)", flush=True)
    state = _load_or_plan(cfg, request, force_plan=force_plan)

    emit("run_started", {
        "repo": str(cfg.repo),
        "prompt": request,
        "base_branch": state.base_branch,
        "test_cmd": state.test_cmd,
    })
    emit("planned", {
        "tasks": [t.to_dict() for t in state.tasks],
        "test_cmd": state.test_cmd,
    })

    pending = state.pending()
    if only is not None:
        target = state.by_id(only)
        if target is None:
            raise RalphError(f"no task with id {only}")
        if target.status in (Status.DONE,):
            raise RalphError(f"task {only} is already done")
        pending = [target]

    _print_plan(state, dry_run)

    for task in pending:
        if cancelled():
            task.status = Status.PENDING  # leave resumable for a later run
            state.save(cfg.state_path)
            emit("task_updated", task.to_dict())
            break
        if not _budget_left(state, cfg):
            task.status = Status.FAILED
            task.last_error = "global iteration budget exhausted"
            state.save(cfg.state_path)
            emit("task_updated", task.to_dict())
            continue
        _run_one(cfg, state, task, dry_run=dry_run, emit=emit)
        emit("task_updated", task.to_dict())

    done = state.done()
    failed = state.failed()
    skipped = [t for t in state.tasks if t.status not in (Status.DONE, Status.FAILED)]
    state.save(cfg.state_path)
    report = RunReport(
        planned=len(state.tasks),
        done=done,
        failed=failed,
        skipped=skipped,
        duration=time.time() - t0,
    )
    _print_report(report)
    emit("run_finished", {
        "status": "done" if report.all_done else ("cancelled" if cancelled() else "failed"),
        "done": len(done),
        "failed": len(failed),
        "skipped": len(skipped),
        "duration": report.duration,
    })
    return report


def _run_one(cfg: Config, state: State, task: Task, *, dry_run: bool, emit: events.Sink) -> None:
    print(f"\n  task {task.id}: {task.title}")
    runner.run_task(cfg, state, task, dry_run=dry_run, sink=emit)
    icon = {"done": "✓", "failed": "✗"}.get(task.status.value, "·")
    suffix = "  (see .ralph/logs)" if task.status == Status.FAILED else ""
    print(f"    {icon} {task.status.value}{suffix}")


def _print_plan(state: State, dry_run: bool) -> None:
    print(f"\nralph: {len(state.tasks)} task(s) | base={state.base_branch} | "
          f"test={state.test_cmd or '(none)'}{' | dry-run' if dry_run else ''}")
    for t in state.tasks:
        marker = "·" if t.status == Status.PENDING else t.status.value[0].upper()
        print(f"   [{marker}] {t.id}. {t.title}")


def _print_report(report: RunReport) -> None:
    print("\n" + "=" * 52)
    print(f"ralph finished in {report.duration:.0f}s")
    print(f"  done   : {len(report.done)}")
    for t in report.done:
        print(f"    ✓ {t.id}. {t.title}")
    print(f"  failed : {len(report.failed)}")
    for t in report.failed:
        tail = (t.last_error or "").strip().splitlines()
        last = tail[-1] if tail else ""
        print(f"    ✗ {t.id}. {t.title}   {('— ' + last) if last else ''}")
    print(f"  skipped: {len(report.skipped)}")
    print("=" * 52)
