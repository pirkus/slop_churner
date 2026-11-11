from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import events, gitutil, prompts
from .agent import run_agent
from .config import Config
from .errors import RalphError
from .state import State, Status, Task


@dataclass
class StageResult:
    ok: bool
    output: str
    returncode: int


def _log(cfg: Config, task: Task, stage: str, text: str) -> Path:
    safe = gitutil.slugify(task.title)
    path = cfg.log_dir / f"task{task.id:02d}-{safe}" / f"{task.iterations:02d}-{stage}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("\n" if text and not text.endswith("\n") else ""))
    return path


def _save(state: State, cfg: Config) -> None:
    state.save(cfg.state_path)


def run_tests(cfg: Config, wt: Path, test_cmd: str | None) -> StageResult:
    cmd = test_cmd
    if not cmd:
        return StageResult(True, "(no test command configured; skipping tests)", 0)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(wt),
            shell=True,
            capture_output=True,
            text=True,
            timeout=cfg.test_timeout,
        )
        out = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        return StageResult(proc.returncode == 0, out.strip(), proc.returncode)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return StageResult(False, f"tests timed out after {cfg.test_timeout}s\n{out}".strip(), -1)


def open_pr(cfg: Config, state: State, task: Task, wt: Path) -> StageResult:
    """Local PR: commit, best-effort push, merge into base. Aborts on conflict."""
    try:
        if not gitutil.commit_all(wt, f"{task.title} (ralph task {task.id})"):
            # Nothing new this iteration; the branch may already hold earlier work.
            pass
        note = None
        if gitutil.has_remote(wt):
            note = gitutil.push(wt, task.branch or "")
        # Merge into the base branch in the main repo.
        gitutil.merge_into_base(cfg.repo, task.branch or "", state.base_branch,
                                f"ralph: {task.title}")
        extra = f"\n{note}" if note else ""
        return StageResult(True, f"merged {task.branch} into {state.base_branch}{extra}", 0)
    except gitutil.GitError as exc:
        # Abort a half-finished merge so base is left clean for the fix attempt.
        _abort_merge(cfg.repo, state.base_branch)
        return StageResult(False, f"PR/merge failed: {exc}", 1)


def _abort_merge(repo: Path, base: str) -> None:
    import subprocess
    subprocess.run(["git", "merge", "--abort"], cwd=str(repo),
                   capture_output=True, text=True)
    # Make sure we are back on base and clean.
    subprocess.run(["git", "checkout", base], cwd=str(repo), capture_output=True, text=True)


def _worktree_path(cfg: Config, task: Task) -> Path:
    return cfg.worktrees_dir / f"task{task.id:02d}-{gitutil.slugify(task.title)}"


def run_task(
    cfg: Config,
    state: State,
    task: Task,
    *,
    dry_run: bool = False,
    sink: events.Sink | None = None,
) -> None:
    emit = sink or events.noop

    def emit_task() -> None:
        emit("task_updated", task.to_dict())

    task.branch = gitutil.branch_name(task.id, task.title)
    wt = _worktree_path(cfg, task)
    task.worktree = str(wt)

    # Ensure base branch is checked out and clean before we branch off it.
    _prepare_base(cfg.repo, state.base_branch)

    # Create (or reuse) a dedicated worktree + branch for this task.
    gitutil.worktree_add(cfg.repo, wt, task.branch, state.base_branch)

    committed_any = False
    last_error = ""
    last_stage = "implement"

    for attempt in range(1, cfg.max_iterations + 1):
        task.iterations = attempt
        is_fix = attempt > 1 and last_error != ""

        # 1) IMPLEMENT / FIX ------------------------------------------------
        if is_fix:
            task.mark(Status.FIXING)
            prompt = prompts.fix_prompt(task, last_stage, last_error)
            stage = f"fix (after {last_stage} failure)"
            step_stage = "fix"
        else:
            task.mark(Status.IMPLEMENTING)
            prompt = prompts.implement_prompt(task)
            stage = "implement"
            step_stage = "implement"
        _save(state, cfg)
        emit_task()

        if dry_run:
            print(f"    [dry-run] would run agent ({'fix' if is_fix else 'implement'}) for task {task.id}")
            return

        print(f"    [{attempt}/{cfg.max_iterations}] {stage}…", flush=True)
        agent = run_agent(cfg, prompt, cwd=wt,
                          label=f"task {task.id}: {stage}")
        _log(cfg, task, step_stage,
             f"$ agent rc={agent.returncode}\n{agent.output}")
        emit("step", {
            "task_id": task.id, "iteration": attempt, "stage": step_stage,
            "ok": agent.ok, "output": agent.output,
            "duration": agent.duration,
        })
        if not agent.ok and agent.returncode in (127, -1):
            # Hard infra failure (missing binary / timeout) - not fixable in loop.
            last_error = agent.output
            last_stage = "implement"
            if attempt >= cfg.max_iterations:
                break
            continue

        committed_any = committed_any or gitutil.commit_all(
            wt, f"{task.title} (ralph task {task.id}, attempt {attempt})"
        )

        # 2) TEST ------------------------------------------------------------
        task.mark(Status.TESTING)
        _save(state, cfg)
        emit_task()
        print(f"    tests: {state.test_cmd or '(none)'}…", flush=True)
        t0 = time.time()
        tests = run_tests(cfg, wt, state.test_cmd)
        _log(cfg, task, "test", tests.output)
        print(f"    tests: {'PASS' if tests.ok else 'FAIL'}", flush=True)
        emit("step", {
            "task_id": task.id, "iteration": attempt, "stage": "test",
            "ok": tests.ok, "output": tests.output,
            "duration": time.time() - t0,
        })
        if not tests.ok:
            last_error = tests.output
            last_stage = "test"
            if attempt >= cfg.max_iterations:
                break
            continue

        # 3) PR (local merge) ------------------------------------------------
        task.mark(Status.PR)
        _save(state, cfg)
        emit_task()
        print(f"    pr: commit + merge {task.branch} → {state.base_branch}…", flush=True)
        t0 = time.time()
        pr = open_pr(cfg, state, task, wt)
        _log(cfg, task, "pr", pr.output)
        print(f"    pr: {'MERGED' if pr.ok else 'FAILED'}", flush=True)
        emit("step", {
            "task_id": task.id, "iteration": attempt, "stage": "pr",
            "ok": pr.ok, "output": pr.output,
            "duration": time.time() - t0,
        })
        if not pr.ok:
            last_error = pr.output
            last_stage = "pr"
            if attempt >= cfg.max_iterations:
                break
            continue

        # 4) DONE ------------------------------------------------------------
        task.status = Status.DONE
        task.last_error = None
        task.summary = (agent.output[-800:] if agent.output else None)
        _cleanup(cfg, state, task, wt)
        _save(state, cfg)
        emit_task()
        return

    # Exhausted iterations.
    task.status = Status.FAILED
    task.last_error = f"stage={last_stage}\n{last_error}"[-4000:]
    if not committed_any:
        task.last_error = "(no changes were ever committed)\n" + (task.last_error or "")
    # Leave the worktree in place for inspection on failure.
    _save(state, cfg)
    emit_task()


def _prepare_base(repo: Path, base: str) -> None:
    if not gitutil.is_clean(repo):
        raise RalphError(
            f"base repo {repo} is not clean; commit or stash before running ralph"
        )
    cur = gitutil.current_branch(repo)
    if cur != base:
        gitutil.checkout_branch(repo, base)


def _cleanup(cfg: Config, state: State, task: Task, wt: Path) -> None:
    gitutil.worktree_remove(cfg.repo, wt)
    # Keep the merged branch history; delete the now-redundant task branch.
    gitutil.delete_branch(cfg.repo, task.branch or "")
    task.worktree = None
