from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class Status(str, Enum):
    PENDING = "pending"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    PR = "pr"
    FIXING = "fixing"
    DONE = "done"
    FAILED = "failed"


TERMINAL = {Status.DONE, Status.FAILED}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: int
    title: str
    description: str
    status: Status = Status.PENDING
    iterations: int = 0
    branch: str | None = None
    worktree: str | None = None
    last_error: str | None = None
    summary: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def mark(self, status: Status) -> None:
        self.status = status
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        d = dict(d)
        d["status"] = Status(d.get("status", "pending"))
        return cls(**d)


@dataclass
class State:
    prompt: str
    base_branch: str
    test_cmd: str | None
    tasks: list[Task] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def load(cls, path: Path) -> "State | None":
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        tasks = [Task.from_dict(t) for t in raw.get("tasks", [])]
        return cls(
            prompt=raw.get("prompt", ""),
            base_branch=raw.get("base_branch", "main"),
            test_cmd=raw.get("test_cmd"),
            tasks=tasks,
            created_at=raw.get("created_at", _now()),
            updated_at=raw.get("updated_at", _now()),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _now()
        payload = {
            "prompt": self.prompt,
            "base_branch": self.base_branch,
            "test_cmd": self.test_cmd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tasks": [t.to_dict() for t in self.tasks],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")

    # Convenience accessors -------------------------------------------------
    def pending(self) -> list[Task]:
        return [t for t in self.tasks if t.status not in TERMINAL]

    def done(self) -> list[Task]:
        return [t for t in self.tasks if t.status == Status.DONE]

    def failed(self) -> list[Task]:
        return [t for t in self.tasks if t.status == Status.FAILED]

    def by_id(self, task_id: int) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)
