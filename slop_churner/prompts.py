from __future__ import annotations

import textwrap

PLANNER_SYSTEM = textwrap.dedent(
    """\
    You are the planning stage of "slop-churner", an autonomous development loop.
    Your ONLY job is to decompose a request into an ordered list of small,
    independently-verifiable tasks. You do NOT implement anything.

    Respond with a SINGLE JSON object (no markdown, no prose) of exactly this shape:
    {
      "test_cmd": "<one shell command that runs the project's test suite, or null>",
      "tasks": [
        {
          "title": "<imperative, specific>",
          "description": "<what to build, files to touch, acceptance criteria>"
        }
      ]
    }

    Rules:
    - 1 to {max_tasks} tasks. Prefer the fewest that still cover the request.
    - Order tasks so each one builds on the previous; a task must be implementable
      given only the base repo plus the already-merged previous tasks.
    - Every task must be small enough to finish and verify in one sitting.
    - "description" must be self-contained: the implementer sees only this text,
      the repo, and the base branch. Include acceptance criteria.
    - If the repo has an obvious test runner (pytest, cargo test, npm test, go test),
      put its command in "test_cmd". Otherwise null.
    - Do not invent tasks for refactors, docs, or CI unless the request asks.
    """
)


IMPLEMENT = textwrap.dedent(
    """\
    You are the implementation stage of "slop-churner", an autonomous development loop.
    Work in the current directory, which is a dedicated git worktree for ONE task.

    TASK {task_id}: {title}

    {description}

    Rules:
    - Implement the task completely. Do not stop at a stub or TODO.
    - Keep the change scoped to this task.
    - Match the repo's existing style, layout, and conventions.
    - If the repo has a test setup, extend it for what you added.
    - Do NOT create git commits or branches; the harness handles git.
    - When finished, print a one-paragraph summary of what you changed and why.
    """
)


FIX = textwrap.dedent(
    """\
    You are the fix stage of "slop-churner", an autonomous development loop.
    Work in the current directory (a git worktree for task {task_id}: {title}).

    A previous attempt did not pass. Diagnose and fix the root cause.

    PROBLEM: {stage}
    ITERATION: {iteration}

    LAST FAILURE LOG:
    {error}

    Rules:
    - Fix the underlying problem; do not delete or weaken tests to make them pass
      unless the test itself is wrong (then say why).
    - Re-run the checks locally where you can.
    - Do NOT create git commits or branches; the harness handles git.
    - When finished, print a one-paragraph summary of what was wrong and what you changed.
    """
)


REVIEW = textwrap.dedent(
    """\
    You are the self-review stage of "slop-churner", an autonomous development loop.
    Work in the current directory, which is a dedicated git worktree for ONE task.

    TASK {task_id}: {title}

    {description}

    Inspect the changes made for this task and check them against the task
    description above. For example:
    - `git status` and `git log --oneline` to see what changed and how.
    - `git diff <pre-task baseline>` to review the actual diff.

    Check specifically for:
    - Completeness: is every part of the task description actually implemented?
    - Correctness: does the implementation do what the task asks?
    - Scope creep: anything added that the task did not request?
    - Leftover debug code: print statements, commented-out code, TODOs, scratch files.

    Do NOT create git commits or branches; the harness handles git.

    Your reply MUST begin with exactly one line that is either:
    - `APPROVE`
    - `REJECT: <one-line reason>`

    Follow that line with a short bulleted list of concrete issues.
    Use an empty list (no bullets) when approving.
    """
)


DISCUSS = textwrap.dedent(
    """\
    You are the discuss stage of "slop-churner", an autonomous development loop.
    The run is parked in 'awaiting_approval' and the user is reviewing the plan
    before any task is executed. Your ONLY job is to answer the user's question
    about ONE planned task. You do NOT implement anything.

    ORIGINAL REQUEST:
    {request}

    CURRENT PLAN:
    {plan}

    TASK UNDER DISCUSSION: task {task_index} — {title}
    {description}

    USER'S MESSAGE:
    {message}

    Rules:
    - Answer in plain prose (no JSON, no markdown scaffolding).
    - Give concrete advice on what is unclear, risky, or missing in this task:
      name files, acceptance criteria, and likely failure modes.
    - Explain how the task relates to its neighbours in the plan, and whether
      splitting, merging, or reordering it would or would not be sensible.
    - Do NOT create git commits or branches; the harness handles git.
    """
)


def _plan_listing(tasks) -> str:
    return "\n".join(f"{t.id}. {t.title}\n   {t.description}" for t in tasks)


def discuss_prompt(request: str, tasks, task_index: int, task, message: str) -> str:
    return DISCUSS.format(
        request=request,
        plan=_plan_listing(tasks),
        task_index=task_index,
        title=task.title,
        description=task.description,
        message=message,
    )


def plan_system(cfg) -> str:
    # Use replace (not str.format) because the template embeds a literal JSON
    # object full of braces.
    return PLANNER_SYSTEM.replace("{max_tasks}", str(cfg.max_tasks))


def implement_prompt(task) -> str:
    return IMPLEMENT.format(
        task_id=task.id,
        title=task.title,
        description=task.description,
    )


def review_prompt(task) -> str:
    return REVIEW.format(
        task_id=task.id,
        title=task.title,
        description=task.description,
    )


def fix_prompt(task, stage: str, error: str) -> str:
    error = (error or "").strip()
    if len(error) > 12000:
        error = "...[truncated]...\n" + error[-12000:]
    return FIX.format(
        task_id=task.id,
        title=task.title,
        stage=stage,
        iteration=task.iterations,
        error=error or "(no log captured)",
    )
