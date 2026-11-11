from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "task"


def branch_name(task_id: int, title: str) -> str:
    return f"slop-churner/{task_id}-{slugify(title)}"


def run_branch_name(run_id: int | None = None) -> str:
    """Staging branch for one slop-churner run; tasks merge here, not into base."""
    return f"slop-churner/run-{run_id}" if run_id is not None else "slop-churner/run"


def branch_exists(repo: Path, branch: str) -> bool:
    return _branch_exists(repo, branch)


def current_branch(cwd: Path) -> str:
    return _run(cwd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def has_remote(cwd: Path) -> bool:
    proc = _run(cwd, "remote", check=False)
    return bool(proc.stdout.strip())


def remote_name(cwd: Path) -> str | None:
    proc = _run(cwd, "remote", check=False)
    remotes = proc.stdout.split()
    return remotes[0] if remotes else None


def is_clean(cwd: Path) -> bool:
    # Ignore slop-churner's own scratch dir so writing state never counts as dirty.
    return not _run(cwd, "status", "--porcelain", "--", ".", ":!.slop-churner").stdout.strip()


def ensure_excluded(cwd: Path, pattern: str = ".slop-churner/") -> None:
    """Make sure slop-churner's scratch dir is git-ignored (via info/exclude)."""
    gitdir = _run(cwd, "rev-parse", "--absolute-git-dir").stdout.strip()
    exclude = Path(gitdir) / "info" / "exclude"
    if not exclude.parent.is_dir():
        exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if any(line.strip() == pattern for line in existing.splitlines()):
        return
    exclude.write_text(existing.rstrip("\n") + f"\n{pattern}\n"
                       if existing.strip() else f"# slop-churner scratch dir\n{pattern}\n")


def _branch_exists(repo: Path, branch: str) -> bool:
    return _run(repo, "rev-parse", "--verify", f"refs/heads/{branch}",
                check=False).returncode == 0


def worktree_add(repo: Path, wt_path: Path, branch: str, start: str) -> None:
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if wt_path.exists() and any(wt_path.iterdir()):
        # Reuse an existing worktree left over from a previous attempt.
        return
    if _branch_exists(repo, branch):
        _run(repo, "worktree", "add", str(wt_path), branch)
    else:
        _run(repo, "worktree", "add", "-b", branch, str(wt_path), start)


def worktree_remove(repo: Path, wt_path: Path) -> None:
    if not wt_path.exists():
        return
    _run(repo, "worktree", "remove", "--force", str(wt_path), check=False)
    _run(repo, "worktree", "prune", check=False)


def worktree_prune(repo: Path) -> None:
    _run(repo, "worktree", "prune", check=False)


def ensure_run_branch(repo: Path, run_branch: str, base: str) -> None:
    """Create the run's staging branch from base if it does not exist yet."""
    if _branch_exists(repo, run_branch):
        return
    _run(repo, "branch", run_branch, base)


def commit_all(cwd: Path, message: str) -> bool:
    """Stage everything and commit. Returns False if there was nothing to commit."""
    _run(cwd, "add", "-A")
    if not _run(cwd, "status", "--porcelain").stdout.strip():
        return False
    _run(cwd, "commit", "-m", message)
    return True


def merge_into(repo: Path, branch: str, target: str, message: str) -> None:
    """Merge `branch` into `target` in the main checkout (the local 'PR')."""
    _run(repo, "checkout", target)
    _run(repo, "merge", "--no-ff", branch, "-m", message)


def delete_branch(repo: Path, branch: str) -> None:
    _run(repo, "branch", "-D", branch, check=False)


def accept_run(repo: Path, run_branch: str, base: str, message: str) -> None:
    """Accept a finished run: merge the staging branch into base, then clean up."""
    if not _branch_exists(repo, run_branch):
        raise GitError(f"staging branch {run_branch} does not exist")
    if current_branch(repo) == run_branch:
        _run(repo, "checkout", base)
    proc = _run(repo, "merge", "--no-ff", run_branch, "-m", message, check=False)
    if proc.returncode != 0 and "Already up to date" not in proc.stdout:
        _run(repo, "merge", "--abort", check=False)
        raise GitError(
            f"merge of {run_branch} into {base} failed: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    _run(repo, "branch", "-D", run_branch, check=False)
    worktree_prune(repo)


def reject_run(repo: Path, run_branch: str, base: str) -> None:
    """Reject a finished run: discard the staging branch and its worktrees."""
    if current_branch(repo) == run_branch:
        _run(repo, "checkout", base)
    _run(repo, "branch", "-D", run_branch, check=False)
    worktree_prune(repo)


def diff_overview(repo: Path, base: str, head: str) -> dict:
    """Per-file stat + patches + commit list for base...head."""
    if not _branch_exists(repo, head):
        return {"files": [], "stat": "(no staging branch yet)",
                "added": 0, "deleted": 0, "commits": []}
    numstat = _run(repo, "diff", "--numstat", f"{base}...{head}").stdout
    patch = _run(repo, "diff", f"{base}...{head}").stdout
    log = _run(repo, "log", "--format=%h %s", f"{base}..{head}").stdout.strip()
    files: list[dict] = []
    added = deleted = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue  # binary file
        a = 0 if parts[0] == "-" else int(parts[0])
        d = 0 if parts[1] == "-" else int(parts[1])
        added += a
        deleted += d
        files.append({"path": parts[2], "added": a, "deleted": d, "patch": ""})
    # Attach each file's hunk to its entry.
    by_path = {f["path"]: f for f in files}
    for chunk in re.split(r"(?=^diff --git )", patch, flags=re.M):
        if not chunk:
            continue
        m = re.match(r"diff --git \S+ (b/.+)", chunk)
        if not m:
            continue
        path = m.group(1)[2:] if m.group(1).startswith("b/") else m.group(1)
        if path in by_path:
            by_path[path]["patch"] = chunk.rstrip("\n")
    return {
        "files": files,
        "stat": f"{len(files)} file(s) changed, +{added} -{deleted}",
        "added": added,
        "deleted": deleted,
        "commits": [c for c in log.splitlines() if c.strip()][:50],
    }


def push(repo: Path, branch: str) -> str | None:
    """Best-effort push if a remote exists. Returns a human note or None."""
    remote = remote_name(repo)
    if not remote:
        return None
    _run(repo, "push", "-u", remote, branch, check=False)
    return f"pushed {branch} to {remote}"


def checkout_branch(cwd: Path, branch: str) -> None:
    _run(cwd, "checkout", branch)
