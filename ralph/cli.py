from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, engine, gitutil
from .config import Config
from .errors import RalphError
from .state import State, Status


def _cfg(args: argparse.Namespace) -> Config:
    return Config(
        repo=Path(args.repo),
        base_branch=args.base,
        agent_cmd=args.agent,
        model=args.model,
        max_iterations=args.max_iterations,
        max_tasks=args.max_tasks,
        budget=args.budget,
        test_cmd=args.test_cmd,
        agent_timeout=args.agent_timeout,
        test_timeout=args.test_timeout,
    )


def _review_gate(cfg: Config, state: State, auto_yes: bool = False) -> str:
    """Present the staged diff and ask whether to accept or reject.

    Returns "accept", "reject" or "keep" (no decision; staging branch kept).
    """
    review = gitutil.diff_overview(cfg.repo, state.base_branch, state.run_branch)
    print(f"\nchanges staged on '{state.run_branch}' "
          f"(not yet in {state.base_branch}):")
    print(f"  {review['stat']}")
    for f in review["files"]:
        print(f"    {f['path']}  (+{f['added']} -{f['deleted']})")
    if auto_yes:
        return "accept"
    try:
        ans = input(f"accept changes into {state.base_branch}? "
                    f"[a]ccept / [r]eject / [Enter] keep for later: ").strip().lower()
    except EOFError:
        ans = ""
    if ans.startswith("a"):
        return "accept"
    if ans.startswith("r"):
        return "reject"
    return "keep"


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if not args.request:
        cfg.prompt_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.prompt_path.write_text(
            "Describe what you want built. ralph will plan tasks, then for each\n"
            "task run an implement -> test -> PR(merge) loop, fixing on failure.\n"
        )
        print(f"no prompt given; wrote a starter to {cfg.prompt_path}")
        print("edit it, then run: ralph run --repo .")
        return 1
    report = engine.run(
        cfg,
        args.request,
        force_plan=args.force_plan,
        dry_run=args.dry_run,
        only=args.task,
    )
    ok = bool(report.all_done)
    # Acceptance gate: work is staged on the run branch, not in base yet.
    if not args.dry_run and report.done and cfg.run_branch:
        state = State.load(cfg.state_path) or State(
            prompt=args.request, base_branch=cfg.base_branch or "main", test_cmd=None)
        if not state.run_branch:
            state.run_branch = cfg.run_branch
        decision = _review_gate(cfg, state, auto_yes=args.yes)
        if decision == "accept":
            engine.accept_run(cfg, state)
            print(f"accepted: staged changes merged into {state.base_branch}")
        elif decision == "reject":
            engine.reject_run(cfg, state)
            print(f"rejected: staging branch {cfg.run_branch} discarded")
        else:
            print(f"kept for later: review with `ralph accept` or `ralph reject` "
                  f"(staging branch {cfg.run_branch})")
            ok = True  # the work is done; the decision is simply pending
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    state = State.load(cfg.state_path)
    if state is None:
        print("no ralph state found in", cfg.repo, "(run: ralph run \"...\")")
        return 1
    print(f"repo : {cfg.repo}")
    print(f"base : {state.base_branch}")
    print(f"test : {state.test_cmd or '(none)'}")
    print(f"prompt: {state.prompt}\n")
    for t in state.tasks:
        extra = ""
        if t.status == Status.FAILED and t.last_error:
            last = t.last_error.strip().splitlines()
            extra = "  " + (last[-1] if last else "")
        print(f"  [{t.status.value:12}] {t.id}. {t.title}  (iter {t.iterations}){extra}")
    return 0 if not state.failed() else 1


def cmd_retry(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    state = State.load(cfg.state_path)
    if state is None:
        print("no state found; nothing to retry")
        return 1
    task = state.by_id(args.task)
    if task is None:
        print(f"no task with id {args.task}")
        return 1
    task.status = Status.PENDING
    task.iterations = 0
    task.last_error = None
    state.save(cfg.state_path)
    report = engine.run(cfg, state.prompt, only=args.task, dry_run=args.dry_run)
    return 0 if report.all_done else 1


def _simple_cfg(args: argparse.Namespace) -> Config:
    """Minimal config for accept/reject: only repo + base matter."""
    return Config(repo=Path(args.repo), base_branch=args.base)


def cmd_accept(args: argparse.Namespace) -> int:
    cfg = _simple_cfg(args)
    state = State.load(cfg.state_path)
    if state is None:
        print("no state found; nothing to accept")
        return 1
    branch = state.run_branch
    engine.accept_run(cfg, state)
    print(f"accepted: {branch} merged into {state.base_branch}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    cfg = _simple_cfg(args)
    state = State.load(cfg.state_path)
    if state is None:
        print("no state found; nothing to reject")
        return 1
    branch = state.run_branch
    engine.reject_run(cfg, state)
    print(f"rejected: staging branch {branch} discarded")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    state = State.load(cfg.state_path)
    if state is None:
        print("no state found")
        return 1
    for t in state.tasks:
        print(f"--- task {t.id}: {t.title} [{t.status.value}] ---")
        print(t.description)
        if t.last_error:
            print("\n  last error:\n" + "\n".join("  " + l for l in t.last_error.splitlines()))
        print()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from . import web
    return web.serve(
        host=args.host,
        port=args.port,
        db=args.db,
        open_browser=args.open,
    )


def cmd_clean(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    state = State.load(cfg.state_path)
    if state is None:
        print("no state found; nothing to clean")
        return 0
    for t in state.tasks:
        if t.worktree:
            wt = Path(t.worktree)
            if wt.exists():
                gitutil.worktree_remove(cfg.repo, wt)
                print("removed worktree", wt)
    print("clean complete (state kept)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ralph",
        description="Ralph: an autonomous implement -> test -> PR loop driven by a coding agent.",
    )
    p.add_argument("--version", action="version", version=f"ralph {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo", default=".", help="target git repository (default: .)")
        sp.add_argument("--base", default=None,
                        help="base branch (default: repo's current branch)")
        sp.add_argument("--agent", default="pi", help="agent binary (default: pi)")
        sp.add_argument("--model", default=None, help="model pattern/ID passed to the agent")
        sp.add_argument("--max-iterations", type=int, default=3,
                        help="implement/test/PR attempts per task (default: 3)")
        sp.add_argument("--max-tasks", type=int, default=8, help="max planned tasks (default: 8)")
        sp.add_argument("--budget", type=int, default=None,
                        help="global cap on total iterations across tasks")
        sp.add_argument("--test-cmd", default=None, help="override the test command")
        sp.add_argument("--agent-timeout", type=int, default=1800, help="seconds per agent call")
        sp.add_argument("--test-timeout", type=int, default=900, help="seconds per test run")
        sp.add_argument("--dry-run", action="store_true", help="plan and print, do not run agents/git")

    sp = sub.add_parser("run", help="plan tasks and execute them")
    sp.add_argument("request", nargs="?", help="what to build (or edit .ralph/PROMPT.md)")
    sp.add_argument("--task", type=int, default=None, help="run only this task id")
    sp.add_argument("--force-plan", action="store_true", help="re-plan even if state exists")
    sp.add_argument("--yes", action="store_true",
                    help="auto-accept the staged changes at the end (no prompt)")
    common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("accept", help="merge the staged run into the base branch")
    sp.add_argument("--repo", default=".", help="target git repository (default: .)")
    sp.add_argument("--base", default=None,
                    help="base branch (default: repo's current branch)")
    sp.set_defaults(func=cmd_accept)

    sp = sub.add_parser("reject", help="discard the staged run (staging branch + worktrees)")
    sp.add_argument("--repo", default=".", help="target git repository (default: .)")
    sp.add_argument("--base", default=None,
                    help="base branch (default: repo's current branch)")
    sp.set_defaults(func=cmd_reject)

    sp = sub.add_parser("status", help="show current state and task progress")
    common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("tasks", help="print full task descriptions")
    common(sp)
    sp.set_defaults(func=cmd_tasks)

    sp = sub.add_parser("retry", help="re-run a single task by id")
    sp.add_argument("task", type=int, help="task id to retry")
    common(sp)
    sp.set_defaults(func=cmd_retry)

    sp = sub.add_parser("clean", help="remove leftover worktrees (keeps state)")
    common(sp)
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("web", help="serve the web dashboard + API")
    sp.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    sp.add_argument("--port", type=int, default=8787, help="port (default: 8787)")
    sp.add_argument("--db", default=None,
                    help="sqlite path (default: $RALPH_DB or ~/.ralph/ralph.db)")
    sp.add_argument("--open", action="store_true", help="open a browser window")
    sp.set_defaults(func=cmd_web)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RalphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
