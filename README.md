# ralph

An autonomous development loop. Give it a prompt; it decomposes the request
into small tasks, then — for each task — runs an **implement → test → PR**
cycle, and on any failure re-enters a **fix** loop until it's green or the
iteration budget is exhausted.

Every step is driven by a fresh-context coding agent (default: [`pi`](https://github.com/badlogic/pi-mono) in
non-interactive `pi -p` mode). There is no persistent session between steps:
the only shared state is the git repo and a small on-disk state file, so the
run is resumable and easy to inspect.

```
        ┌──────────────────────────────────────────────────────────────┐
        │                          ralph run "…"                       │
        └──────────────────────────────────────────────────────────────┘
                                   │
                          ┌────────▼────────┐
                          │   PLANNER agent │  reads the repo, emits tasks.json
                          └────────┬────────┘
                                   │  N small, ordered, independently-testable tasks
        ┌──────────────────────────┴───────────────────────────────────────┐
        │ for each task (sequentially, on a dedicated worktree + branch)   │
        │                                                                  │
        │   ┌─────────┐   ┌─────────┐   ┌─────────┐                        │
        │   │IMPL/FIX │──▶│  TEST   │──▶│   PR    │──▶ merged? ──▶ next task
        │   └─────────┘   └─────────┘   └─────────┘     │                 │
        │        ▲                                        │ no (fail)       │
        │        └────────────────────────────────────────┘                │
        │              (fix loop, up to --max-iterations)                 │
        └──────────────────────────────────────────────────────────────────┘
```

## Why "ralph"?

It follows the [Ralph](https://ghuntley.com/ralph/) idea: instead of one long,
context-bloated agent session, you run many short, focused agent invocations
against a shared, durable artifact (here: the repo + a state file). Each
invocation starts clean, does one job, and hands off. That makes the loop
resilient to context rot, cheap to retry, and trivially resumable.

## Install

```bash
pip install -e .            # puts a `ralph` entrypoint on PATH
# or, without installing:
python3 -m ralph --help                        # from the project root
python3 /path/to/ralph/ralph-cli.py --help     # standalone launcher, from anywhere
```

Requires Python ≥ 3.10, `git`, and an agent binary on `PATH` (default `pi`).
No third-party Python dependencies.

## Quick start

```bash
cd /path/to/your/project
ralph run "Add rate limiting to the /api/login endpoint using a token bucket"
```

What happens:

1. **Plan** — an agent inspects your repo and writes `.ralph/tasks.json`:
   an ordered list of small, self-contained tasks plus a `test_cmd`.
2. **Per task** — ralph creates a worktree + branch (`ralph/<id>-<slug>`)
   off your base branch, then loops:
   - **implement/fix** — a fresh agent implements (or fixes) the task.
   - **test** — ralph runs the test command in the worktree.
   - **PR** — commits, best-effort pushes (if a remote exists), and merges the
     branch into your base branch. This local merge *is* the PR gate.
   - If test or PR fails, the failure log is fed back into the **fix** step.
3. **Done** — the worktree and task branch are cleaned up; state is updated.

A failed task keeps its worktree (under `<repo>-ralph/`) so you can inspect it.

## Commands

```bash
ralph run "PROMPT"            # plan (if needed) and execute all tasks
ralph run "PROMPT" --dry-run  # plan and print the task list, do nothing else
ralph run "PROMPT" --task 3   # run only task #3
ralph run "PROMPT" --force-plan
                              # re-plan even if .ralph/state.json exists

ralph status                  # show state, per-task progress, last errors
ralph tasks                   # print full task descriptions + last error
ralph retry 3                 # reset and re-run task #3
ralph clean                   # remove leftover worktrees (keeps state)
ralph web                     # serve the web dashboard + API (see below)
```

## Web UI

`ralph web` starts a local dashboard (stdlib only, no extra dependencies) where
you can create runs, watch them progress live, and read every step's log:

```bash
ralph web                          # http://127.0.0.1:8787
ralph web --port 9000 --open       # different port, open a browser
ralph web --db /path/to/ralph.db   # custom database location
```

Features:

- **New run form** — a prompt plus all the usual parameters (repo, base
  branch, agent, model, max iterations, max tasks, budget, test command,
  timeouts).
- **Live progress** — runs execute in the background; task statuses and step
  logs update in real time over Server-Sent Events (no page refresh).
- **Step logs** — expand any task to see each iteration's `implement`/`fix`,
  `test` and `pr` transcript with pass/fail and duration.
- **Cancel & retry** — cancel an active run (takes effect at the next safe
  point), or retry a failed task from its row.

History is kept in a local SQLite database (`$RALPH_DB`, else
`~/.ralph/ralph.db`) with `runs`, `tasks` and `steps` tables, so old runs and
their logs stay available after a server restart. The UI and CLI share the
same engine; the CLI's `.ralph/` artifacts (state file, file logs, worktrees)
are unchanged.

A small JSON API is available for scripting:

```bash
GET  /api/runs                        # list runs
POST /api/runs                        # {prompt, repo, base, agent, ...} -> {run_id}
GET  /api/runs/<id>                   # run + tasks
GET  /api/runs/<id>/tasks/<tid>       # task + step logs
GET  /api/runs/<id>/events            # SSE stream of live events
POST /api/runs/<id>/cancel            # cancel an active run
POST /api/runs/<id>/retry/<tid>       # re-run a failed task
```

Note: only one active run per repo is allowed (the state file and git repo
are shared), enforced by the server.

### Common flags (all subcommands)

| flag | default | meaning |
| --- | --- | --- |
| `--repo` | `.` | target git repository |
| `--base` | `main` | base branch to branch off / merge into |
| `--agent` | `pi` | agent binary to invoke |
| `--model` | — | model pattern/ID passed to the agent |
| `--max-iterations` | `3` | implement→test→PR attempts per task |
| `--max-tasks` | `8` | cap on how many tasks the planner may emit |
| `--budget` | ∞ | global cap on total iterations across all tasks |
| `--test-cmd` | planner's | override the test command |
| `--agent-timeout` | `1800` | seconds per agent call |
| `--test-timeout` | `900` | seconds per test run |
| `--dry-run` | off | plan + print only |

You can also set `RALPH_AGENT` instead of passing `--agent`.

## State & artifacts

Everything ralph writes lives in two places, both easy to delete:

- **`.ralph/`** inside the target repo (auto-added to `.git/info/exclude`):
  - `state.json` — task list, statuses, iteration counts, last errors.
  - `tasks.json` — the planner's raw output.
  - `logs/taskNN-<slug>/NN-<stage>.log` — every agent/test/PR transcript.
- **`<repo>-ralph/`** as a sibling of the target repo: the per-task worktrees
  (only surviving ones from failed tasks).

To start completely fresh: `ralph clean` then delete `.ralph/`.

## The local "PR"

`ralph` is git-native and works fully offline. The **PR** step is:

1. commit the worktree's changes,
2. `git push -u <remote> <branch>` if a remote is configured (best effort),
3. `git merge --no-ff <branch>` into your base branch.

If you want a *real* PR/CI gate (e.g. GitHub Actions), the cleanest extension
is to replace step 3 with "open PR, wait for checks, merge on green" — see
`ralph/runner.py::open_pr`, which is the single place that models the PR.

## Design notes

- **Fresh context per step.** Each agent call is a separate process with no
  session history. The only things it sees are: its role prompt, the task
  spec (or the previous failure log), and the repo. This is what keeps long
  multi-task runs from degrading.
- **One task at a time, strictly sequential.** Each task merges into base
  before the next starts, so task N+1 is implemented against a repo that
  already contains 1..N. That matches the planner's contract and avoids
  cross-task merge tangles.
- **The fix loop is the inner loop.** `test` and `PR` are gates; failing
  either one routes back to the `fix` agent with the captured failure log,
  up to `--max-iterations`.
- **Failures are inspectable, not destructive.** A failed task's worktree is
  left in place and its last error is recorded in `state.json`.

## Limitations / TODO

- Sequential by design; parallel task execution is a natural next step (the
  planner already emits independent tasks).
- The "PR" is a local merge; wire in real CI checks if you need them.
- No cost/token budgeting yet (only wall-clock timeouts and iteration caps).
- The planner is a single agent call; a critic/refine pass could improve task
  quality.

## Development

```bash
python3 tests/smoke.py        # offline end-to-end test (agent is stubbed)
python3 tests/web_smoke.py    # web API + store end-to-end test (agent stubbed)
```
