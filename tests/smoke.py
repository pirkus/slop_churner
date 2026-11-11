"""Offline smoke test: drives the whole ralph loop with a stubbed agent.

Run:  python3 tests/smoke.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ralph import planner, runner  # noqa: E402
from ralph.config import Config  # noqa: E402
from ralph.engine import run  # noqa: E402
from ralph.agent import AgentResult  # noqa: E402


def sh(cwd, *a):
    subprocess.run(a, cwd=str(cwd), check=True, capture_output=True)


def make_repo(root: Path) -> None:
    sh(root, "git", "init", "-b", "main")
    sh(root, "git", "config", "user.email", "ralph@test")
    sh(root, "git", "config", "user.name", "ralph")
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
    tmp = Path(tempfile.mkdtemp(prefix="ralph-smoke-"))
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

    # The change must be merged into main in the base repo.
    sh(repo, "git", "log", "--oneline", "-5")
    main_has_sub = "def subtract" in (repo / "app.py").read_text()
    assert main_has_sub, "subtract should be merged into main"

    # No leftover worktrees.
    wts = subprocess.run(["git", "worktree", "list"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    assert wts.count("\n") <= 1, f"expected no extra worktrees, got:\n{wts}"

    # The task branch should have been deleted after merge.
    branches = subprocess.run(["git", "branch"], cwd=str(repo),
                              capture_output=True, text=True).stdout
    assert "ralph/" not in branches, f"task branch should be deleted:\n{branches}"

    print("\nSMOKE TEST PASSED")
    return 0


def main_plan_supplied() -> int:
    """engine.run with a pre-supplied planner.Plan: planner agent must never run."""
    tmp = Path(tempfile.mkdtemp(prefix="ralph-smoke-plan-"))
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

    # Both supplied tasks must have been executed and merged into main.
    sh(repo, "git", "log", "--oneline", "-5")
    main_src = (repo / "app.py").read_text()
    assert "def subtract" in main_src and "def multiply" in main_src, \
        "both supplied tasks should be merged into main"

    # No leftover worktrees.
    wts = subprocess.run(["git", "worktree", "list"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    assert wts.count("\n") <= 1, f"expected no extra worktrees, got:\n{wts}"

    # Task branches should have been deleted after merge.
    branches = subprocess.run(["git", "branch"], cwd=str(repo),
                              capture_output=True, text=True).stdout
    assert "ralph/" not in branches, f"task branches should be deleted:\n{branches}"

    print("\nSMOKE TEST (pre-supplied plan) PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or main_plan_supplied())
