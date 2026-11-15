"""Offline test for slop_churner.prompts builders.

Run:  python3 tests/prompts_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import slop_churner.prompts as prompts  # noqa: E402


class FakeTask:
    id = 7
    title = "Add feature X"
    description = "Do Y and Z, with acceptance criteria."
    iterations = 2


class FakeConfig:
    max_tasks = 5


def check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)
    print(f"ok - {name}")


def main() -> None:
    task = FakeTask()

    # plan_system is unchanged and still substitutes max_tasks.
    plan = prompts.plan_system(FakeConfig())
    check("plan_system contains max_tasks", "5" in plan)
    check("plan_system is str", isinstance(plan, str))

    # implement_prompt is unchanged.
    impl = prompts.implement_prompt(task)
    check("implement_prompt has id", "TASK 7:" in impl)
    check("implement_prompt has title", "Add feature X" in impl)
    check("implement_prompt has description", "Do Y and Z" in impl)

    # fix_prompt is unchanged.
    fix = prompts.fix_prompt(task, "implement", "boom")
    check("fix_prompt has error", "boom" in fix)
    check("fix_prompt has iteration", "ITERATION: 2" in fix)

    # review_prompt is the new builder.
    review = prompts.review_prompt(task)
    check("review_prompt is str", isinstance(review, str))
    check("review_prompt has task id", "7" in review)
    check("review_prompt has title", "Add feature X" in review)
    check("review_prompt has description", "Do Y and Z, with acceptance criteria." in review)
    check("review_prompt mentions APPROVE", "APPROVE" in review)
    check("review_prompt mentions REJECT", "REJECT" in review)
    check("review_prompt is self-review stage", "self-review stage" in review)
    check("review_prompt forbids commits", "Do NOT create git commits or branches" in review)
    check("review_prompt has no leftover format placeholders",
          "{" not in review.replace("{task_id}", "").replace("{title}", "")
          .replace("{description}", ""))

    # discuss_prompt is the new builder.
    task2 = FakeTask()
    task2.id = 8
    task2.title = "Add feature Y"
    task2.description = "Do Q, with acceptance criteria."
    discuss = prompts.discuss_prompt(
        "add a subtract function", [task, task2], 1, task, "why split this?")
    check("discuss_prompt mentions discuss stage", "discuss stage" in discuss)
    check("discuss_prompt has the request", "add a subtract function" in discuss)
    check("discuss_prompt lists every planned task", "Add feature Y" in discuss)
    check("discuss_prompt names the discussed task", "task 1" in discuss)
    check("discuss_prompt has the task title", "Add feature X" in discuss)
    check("discuss_prompt has the user message", "why split this?" in discuss)
    check("discuss_prompt asks for prose advice", "plain prose" in discuss)
    check("discuss_prompt covers neighbours", "neighbours" in discuss)

    print("All prompts tests passed.")


if __name__ == "__main__":
    main()
