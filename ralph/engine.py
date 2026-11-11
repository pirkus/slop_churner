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
    plan: planner.Plan | None = None,
) -> RunReport:
    emit = sink or events.noop
    cancelled = is_cancelled or (lambda: False)
    t0 = time.time()
    gitutil.ensure_excluded(cfg.repo)
    if plan is not None:
        # Pre-supplied plan: build the state directly, no planner agent.
        state = planner.build_state(cfg, request, plan)
        state.save(cfg.state_path)
    else:
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

    # Ensure the run's staging branch exists; tasks merge into it, and only
    # an explicit accept (see accept_run) lands the work in the base branch.
    if not dry_run:
        if not cfg.run_branch:
            cfg.run_branch = state.run_branch or gitutil.run_branch_name()
        state.run_branch = cfg.run_branch
        if not gitutil.branch_exists(cfg.repo, cfg.run_branch):
            gitutil.ensure_run_branch(cfg.repo, cfg.run_branch, state.base_branch)
            print(f"ralph: staging branch {cfg.run_branch} created from "
                  f"{state.base_branch} (base stays clean until you accept)")
        else:
            print(f"ralph: continuing existing staging branch {cfg.run_branch}")
        state.save(cfg.state_path)

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
    # Leave the main checkout on the base branch; the staged work lives on
    # its own branch until the user accepts or rejects the run.
    if not dry_run and cfg.run_branch and gitutil.branch_exists(cfg.repo, cfg.run_branch):
        if gitutil.current_branch(cfg.repo) == cfg.run_branch:
            try:
                gitutil.checkout_branch(cfg.repo, state.base_branch)
            except gitutil.GitError:
                pass  # dirty tree; the staged branch stays checked out
    state.save(cfg.state_path)
    report = RunReport(
        planned=len(state.tasks),
        done=done,
        failed=failed,
        skipped=skipped,
        duration=time.time() - t0,
    )
    _print_report(report)
    if report.done and not dry_run:
        review = gitutil.diff_overview(cfg.repo, state.base_branch, cfg.run_branch or "")
        print(f"\nralph: {len(report.done)} task(s) staged on '{cfg.run_branch}'.")
        print(f"      {review['stat']}")
        for f in review["files"]:
            print(f"        {f['path']}  (+{f['added']} -{f['deleted']})")
        print("      review the diff, then:  ralph accept   or   ralph reject")
        emit("run_review", {
            "run_branch": cfg.run_branch,
            "base_branch": state.base_branch,
            "stat": review["stat"],
            "files": review["files"],
            "commits": review["commits"],
        })
    emit("run_finished", {
        "status": "done" if report.all_done else ("cancelled" if cancelled() else "failed"),
        "done": len(done),
        "failed": len(failed),
        "skipped": len(skipped),
        "duration": report.duration,
    })
    return report


def accept_run(cfg: Config, state: State) -> None:
    """Merge the run's staging branch into base and clean up (the 'accept')."""
    if not state.run_branch:
        raise RalphError("no staged run found in state (nothing to accept)")
    gitutil.accept_run(cfg.repo, state.run_branch, state.base_branch,
                       f"ralph: accept run ({state.prompt[:60]})")
    state.run_branch = None
    state.save(cfg.state_path)


def reject_run(cfg: Config, state: State) -> None:
    """Discard the run's staging branch and worktrees (the 'reject')."""
    if not state.run_branch:
        raise RalphError("no staged run found in state (nothing to reject)")
    gitutil.reject_run(cfg.repo, state.run_branch, state.base_branch)
    state.run_branch = None
    state.save(cfg.state_path)


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
