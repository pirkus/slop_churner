from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    repo: Path
    base_branch: str | None = None  # None = auto-detect current branch
    agent_cmd: str = os.environ.get("RALPH_AGENT", "pi")
    model: str | None = None
    max_iterations: int = 3          # implement/test/PR attempts per task
    max_tasks: int = 8               # cap on planned tasks
    budget: int | None = None        # global cap on agent calls (None = unlimited)
    test_cmd: str | None = None      # override; default comes from tasks.json / autodetect
    pr_check_cmd: str | None = None  # optional hook run before merge (e.g. CI checks)
    agent_timeout: int = 1800        # seconds per agent call
    test_timeout: int = 900          # seconds per test run

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()
        if not (self.repo / ".git").exists():
            from .errors import RalphError
            raise RalphError(f"{self.repo} is not a git repository")
        if not self.base_branch:
            import subprocess
            r = subprocess.run(
                ["git", "-C", str(self.repo), "symbolic-ref", "--short", "HEAD"],
                capture_output=True, text=True,
            )
            self.base_branch = r.stdout.strip() or "main"

    @property
    def ralph_dir(self) -> Path:
        return self.repo / ".ralph"

    @property
    def log_dir(self) -> Path:
        return self.ralph_dir / "logs"

    @property
    def worktrees_dir(self) -> Path:
        return self.repo.parent / f"{self.repo.name}-ralph"

    @property
    def state_path(self) -> Path:
        return self.ralph_dir / "state.json"

    @property
    def tasks_path(self) -> Path:
        return self.ralph_dir / "tasks.json"

    @property
    def prompt_path(self) -> Path:
        return self.ralph_dir / "PROMPT.md"
