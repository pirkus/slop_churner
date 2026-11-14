"""Offline smoke test: drives the whole slop-churner loop with a stubbed agent.

Run:  python3 tests/smoke.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slop_churner import planner, runner  # noqa: E402
from slop_churner.config import Config  # noqa: E402
from slop_churner.engine import run  # noqa: E402
from slop_churner.agent import AgentResult  # noqa: E402


def sh(cwd, *a):
    subprocess.run(a, cwd=str(cwd), check=True, capture_output=True)


def make_repo(root: Path) -> None:
    sh(root, "git", "init", "-b", "main")
    sh(root, "git", "config", "user.email", "slop-churner@test")
    sh(root, "git", "config", "user.name", "slop-churner")
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "test_app.py").write_text(
        "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-m", "init")


PLAN_JSON = """
{
  "test_cmd": "python3 -m pytest -q || python3 -m unittest discover -q || true",
  "tasks": [
    {
      "title": "Add a subtract function",
      "description": "Add subtract(a, b) to app.py returning a - b, with a test."
    }
  ]
}
"""


class StubAgent:
    """Fake agent: planner returns JSON; implementer edits a file; tests flip once."""

    def __init__(self):
        self.calls = 0
        self.test_attempts = 0

    def __call__(self, cfg, prompt, *, cwd, tools=None, timeout=None, **kw):
        self.calls += 1
        if "planning stage" in prompt:
            return AgentResult(True, PLAN_JSON, 0, 0.1)
        # implement / fix
        app = cwd / "app.py"
        if "subtract" not in app.read_text():
            app.write_text(
                "def add(a, b):\n    return a + b\n\n"
                "def subtract(a, b):\n    return a - b\n"
            )
            (cwd / "test_app.py").write_text(
                "from app import add, subtract\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_sub():\n    assert subtract(5, 2) == 3\n"
            )
        return AgentResult(True, "implemented subtract", 0, 0.1)


def stub_tests(cfg, wt, test_cmd):
    # First test run fails (simulates a red build), second passes.
    stub_tests.n = getattr(stub_tests, "n", 0) + 1
    if stub_tests.n == 1:
        return runner.StageResult(False, "AssertionError: subtract missing", 1)
    return runner.StageResult(True, "2 passed", 0)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="slop-churner-smoke-"))
    repo = tmp / "proj"
    repo.mkdir()
    make_repo(repo)

    cfg = Config(repo=repo, base_branch="main", agent_cmd="pi",
                 max_iterations=3, test_cmd="true")
    stub = StubAgent()
    planner.run_agent = stub          # patch where it's used
    runner.run_agent = stub
    runner.run_tests = stub_tests     # exercise the fix loop

    report = run(cfg, "add a subtract function")
    print("report.all_done =", report.all_done)
    print("report.done     =", [t.title for t in report.done])
    print("report.failed   =", [t.title for t in report.failed])

    # Assertions -------------------------------------------------------------
    assert report.all_done, "expected all tasks to complete"
    assert len(report.done) == 1

    # The change must be STAGED on the run branch, not yet in main.
    branches = subprocess.run(["git", "branch"], cwd=str(repo),
                              capture_output=True, text=True).stdout
    assert "slop-churner/run" in branches, f"staging branch should exist:\n{branches}"
    sh(repo, "git", "checkout", "slop-churner/run")
    staged = "def subtract" in (repo / "app.py").read_text()
    assert staged, "subtract should be staged on the run branch"
    sh(repo, "git", "checkout", "main")
    assert "def subtract" not in (repo / "app.py").read_text(), \
        "main must stay clean until the run is accepted"

    # Accept the run: work lands in main, staging + task branches cleaned up.
    from slop_churner import engine
    from slop_churner.state import State
    state = State.load(cfg.state_path)
    assert state is not None and state.run_branch == "slop-churner/run", \
        "state must record the staging branch"
    engine.accept_run(cfg, state)
    assert "def subtract" in (repo / "app.py").read_text(), \
        "subtract should be merged into main after accept"
    sh(repo, "git", "log", "--oneline", "-5")

    # No leftover worktrees or slop-churner branches.
    wts = subprocess.run(["git", "worktree", "list"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    assert wts.count("\n") <= 1, f"expected no extra worktrees, got:\n{wts}"
    branches = subprocess.run(["git", "branch"], cwd=str(repo),
                              capture_output=True, text=True).stdout
    assert "slop-churner/" not in branches, f"branches should be cleaned up:\n{branches}"
    assert state.run_branch is None, "state must clear run_branch after accept"

    # pr_enabled: default is False; CLI --pr sets it; web params thread it through.
    assert cfg.pr_enabled is False, "pr_enabled must default to False"
    from slop_churner.cli import build_parser
    args = build_parser().parse_args(["run", "x"])
    assert args.pr is False, "--pr must default to False"
    args = build_parser().parse_args(["run", "x", "--pr"])
    assert args.pr is True, "--pr must be a store_true flag"
    from slop_churner.web import _build_cfg
    assert _build_cfg({"repo": repo, "base": "main"}).pr_enabled is False
    assert _build_cfg({"repo": repo, "base": "main", "pr_enabled": True}).pr_enabled is True

    # Reject path on a fresh repo: staging work is discarded, base untouched.
    tmp2 = Path(tempfile.mkdtemp(prefix="slop-churner-smoke-reject-"))
    repo2 = tmp2 / "proj"
    repo2.mkdir()
    make_repo(repo2)
    cfg2 = Config(repo=repo2, base_branch="main", agent_cmd="pi",
                  max_iterations=3, test_cmd="true")
    planner.run_agent = stub
    runner.run_agent = stub
    stub_tests.n = 1  # start green
    r2 = run(cfg2, "add a subtract function")
    assert r2.all_done
    state2 = State.load(cfg2.state_path)
    assert state2.run_branch == "slop-churner/run"

    # diff_overview should report the staged change while it is pending.
    from slop_churner import gitutil
    ov = gitutil.diff_overview(repo2, "main", "slop-churner/run")
    assert any(f["path"] == "app.py" and f["added"] > 0 for f in ov["files"]), ov
    assert ov["added"] > 0 and ov["deleted"] >= 0
    assert any("app.py" in f["patch"] for f in ov["files"] if f["patch"])
    assert ov["commits"], "commit list should not be empty"
    # Missing-branch path degrades gracefully.
    ov2 = gitutil.diff_overview(repo2, "main", "slop-churner/nope")
    assert ov2["files"] == [] and ov2["added"] == 0

    engine.reject_run(cfg2, state2)
    branches = subprocess.run(["git", "branch"], cwd=str(repo2),
                              capture_output=True, text=True).stdout
    assert "slop-churner/" not in branches, f"staging branch should be gone:\n{branches}"
    assert "def subtract" not in (repo2 / "app.py").read_text(), \
        "main must be untouched after reject"
    wts = subprocess.run(["git", "worktree", "list"], cwd=str(repo2),
                         capture_output=True, text=True).stdout
    assert wts.count("\n") <= 1, f"worktrees should be cleaned up, got:\n{wts}"

    print("\nSMOKE TEST PASSED")
    return 0


def main_plan_supplied() -> int:
    """engine.run with a pre-supplied planner.Plan: planner agent must never run."""
    tmp = Path(tempfile.mkdtemp(prefix="slop-churner-smoke-plan-"))
    repo = tmp / "proj"
    repo.mkdir()
    make_repo(repo)

    cfg = Config(repo=repo, base_branch="main", agent_cmd="pi",
                 max_iterations=3, test_cmd="true")

    plan = planner.Plan(
        test_cmd=None,
        tasks=[
            planner.Task(id=1, title="Add a subtract function",
                         description="Add subtract(a, b) to app.py returning a - b, with a test."),
            planner.Task(id=2, title="Add a multiply function",
                         description="Add multiply(a, b) to app.py returning a * b, with a test."),
        ],
    )

    class ImplStub:
        """Fake implementer/fixer agent; counts any planner-stage prompts it sees."""

        def __init__(self):
            self.planner_calls = 0

        def __call__(self, cfg, prompt, *, cwd, tools=None, timeout=None, **kw):
            if "planning stage" in prompt:
                self.planner_calls += 1
                raise AssertionError(
                    "planner agent must not be invoked with a pre-supplied plan"
                )
            app = cwd / "app.py"
            src = app.read_text()
            if "subtract" not in src:
                src += "\n\ndef subtract(a, b):\n    return a - b\n"
            if "multiply" not in src:
                src += "\n\ndef multiply(a, b):\n    return a * b\n"
            app.write_text(src)
            return AgentResult(True, "implemented", 0, 0.1)

    stub = ImplStub()
    planner.run_agent = stub          # patch where it's used (should never fire)
    runner.run_agent = stub
    stub_tests.n = 0                  # reset red-once counter
    runner.run_tests = stub_tests

    report = run(cfg, "add subtract and multiply", plan=plan)

    # Assertions -------------------------------------------------------------
    assert stub.planner_calls == 0, "planner agent must not be called"
    assert report.all_done, "expected all tasks to complete"
    assert len(report.done) == 2
    assert [t.id for t in report.done] == [1, 2]

    # Both supplied tasks must be staged on the run branch...
    branches = subprocess.run(["git", "branch"], cwd=str(repo),
                              capture_output=True, text=True).stdout
    assert "slop-churner/run" in branches, f"staging branch should exist:\n{branches}"
    sh(repo, "git", "checkout", "slop-churner/run")
    staged_src = (repo / "app.py").read_text()
    assert "def subtract" in staged_src and "def multiply" in staged_src, \
        "both supplied tasks should be staged on the run branch"
    sh(repo, "git", "checkout", "main")
    assert "def subtract" not in (repo / "app.py").read_text(), \
        "main must stay clean until accept"

    # ...and accept lands both in main (task 2 built on task 1's work).
    from slop_churner import engine
    from slop_churner.state import State
    state = State.load(cfg.state_path)
    engine.accept_run(cfg, state)
    main_src = (repo / "app.py").read_text()
    assert "def subtract" in main_src and "def multiply" in main_src, \
        "both supplied tasks should be merged into main after accept"

    # No leftover worktrees or branches.
    wts = subprocess.run(["git", "worktree", "list"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    assert wts.count("\n") <= 1, f"expected no extra worktrees, got:\n{wts}"
    branches = subprocess.run(["git", "branch"], cwd=str(repo),
                              capture_output=True, text=True).stdout
    assert "slop-churner/" not in branches, f"branches should be cleaned up:\n{branches}"

    print("\nSMOKE TEST (pre-supplied plan) PASSED")
    return 0


def main_streaming() -> int:
    """run_agent streams stdout lines to on_output as they arrive."""
    from slop_churner import agent

    tmp = Path(tempfile.mkdtemp(prefix="slop-churner-stream-"))
    # agent_cmd must accept the "-p <prompt>" args slop-churner passes it.
    script = tmp / "fake_agent"
    script.write_text("#!/bin/sh\necho a\nsleep 0.3\necho b\n")
    script.chmod(0o755)
    err_script = tmp / "fake_err"
    err_script.write_text("#!/bin/sh\necho out\n1>&2 echo err-line\nexit 3\n")
    err_script.chmod(0o755)
    slow_script = tmp / "fake_slow"
    slow_script.write_text("#!/bin/sh\necho early\nsleep 5\n")
    slow_script.chmod(0o755)

    repo = tmp / "proj"
    repo.mkdir()
    make_repo(repo)
    cfg = Config(repo=repo, base_branch="main", agent_cmd=str(script))


    # 1) on_output receives every line, in order, BEFORE the process exits.
    seen: list[tuple[str, float]] = []
    t0 = time.time()
    r = agent.run_agent(cfg, "hello", cwd=tmp, label="stream test", quiet=True,
                        on_output=lambda line: seen.append((line, time.time())))
    t1 = time.time()
    assert r.ok and r.returncode == 0, r
    assert r.output == "a\nb", r.output
    assert [ln for ln, _ in seen] == ["a\n", "b\n"], seen
    # 'a' arrives while the child is still in its sleep (i.e. it is truly
    # streamed, not buffered until exit), and both lines precede the return.
    assert seen[0][1] - t0 < 0.25, "first line must arrive before the 0.3s sleep ends"
    assert all(t < t1 for _, t in seen)

    # 2) on_output=None keeps the old contract (stdout + [stderr] suffix).
    cfg2 = Config(repo=tmp / "proj", base_branch="main", agent_cmd=str(err_script))
    r2 = agent.run_agent(cfg2, "hello", cwd=tmp, label="err test", quiet=True)
    assert not r2.ok and r2.returncode == 3, r2
    assert r2.output == "out\n\n[stderr]\nerr-line", r2.output

    # 3) timeout kills the process and returns returncode -1.
    cfg3 = Config(repo=tmp / "proj", base_branch="main", agent_cmd=str(slow_script))
    streamed: list[str] = []
    r3 = agent.run_agent(cfg3, "hello", cwd=tmp, label="slow", quiet=True,
                         timeout=1, on_output=streamed.append)
    assert not r3.ok and r3.returncode == -1, r3
    assert "agent timed out after 1s" in r3.output, r3.output
    assert r3.output.endswith("early"), r3.output
    assert streamed == ["early\n"], streamed

    # 4) missing binary still maps to 127.
    cfg4 = Config(repo=tmp / "proj", base_branch="main", agent_cmd="definitely-not-a-real-agent")
    r4 = agent.run_agent(cfg4, "hello", cwd=tmp, label="missing", quiet=True)
    assert not r4.ok and r4.returncode == 127, r4
    assert "agent command not found" in r4.output, r4.output

    print("\nSMOKE TEST (streaming) PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or main_plan_supplied() or main_streaming())
