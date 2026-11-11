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
    return f"ralph/{task_id}-{slugify(title)}"


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
    # Ignore ralph's own scratch dir so writing state never counts as dirty.
    return not _run(cwd, "status", "--porcelain", "--", ".", ":!.ralph").stdout.strip()


def ensure_excluded(cwd: Path, pattern: str = ".ralph/") -> None:
    """Make sure ralph's scratch dir is git-ignored (via info/exclude)."""
    gitdir = _run(cwd, "rev-parse", "--absolute-git-dir").stdout.strip()
    exclude = Path(gitdir) / "info" / "exclude"
    if not exclude.parent.is_dir():
        exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if any(line.strip() == pattern for line in existing.splitlines()):
        return
    exclude.write_text(existing.rstrip("\n") + f"\n{pattern}\n"
                       if existing.strip() else f"# ralph scratch dir\n{pattern}\n")


def _branch_exists(repo: Path, branch: str) -> bool:
    return _run(repo, "rev-parse", "--verify", f"refs/heads/{branch}",
                check=False).returncode == 0


def worktree_add(repo: Path, wt_path: Path, branch: str, base: str) -> None:
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    if wt_path.exists() and any(wt_path.iterdir()):
        # Reuse an existing worktree left over from a previous attempt.
        return
    if _branch_exists(repo, branch):
        _run(repo, "worktree", "add", str(wt_path), branch)
    else:
        _run(repo, "worktree", "add", "-b", branch, str(wt_path), base)


def worktree_remove(repo: Path, wt_path: Path) -> None:
    if not wt_path.exists():
        return
    _run(repo, "worktree", "remove", "--force", str(wt_path), check=False)
    _run(repo, "worktree", "prune", check=False)


def commit_all(cwd: Path, message: str) -> bool:
    """Stage everything and commit. Returns False if there was nothing to commit."""
    _run(cwd, "add", "-A")
    if not _run(cwd, "status", "--porcelain").stdout.strip():
        return False
    _run(cwd, "commit", "-m", message)
    return True


def merge_into_base(repo: Path, branch: str, base: str, message: str) -> None:
    """Merge the task branch into the base branch (the local 'PR')."""
    _run(repo, "checkout", base)
    _run(repo, "merge", "--no-ff", branch, "-m", message)


def delete_branch(repo: Path, branch: str) -> None:
    _run(repo, "branch", "-D", branch, check=False)


def push(repo: Path, branch: str) -> str | None:
    """Best-effort push if a remote exists. Returns a human note or None."""
    remote = remote_name(repo)
    if not remote:
        return None
    _run(repo, "push", "-u", remote, branch, check=False)
    return f"pushed {branch} to {remote}"


def checkout_branch(cwd: Path, branch: str) -> None:
    _run(cwd, "checkout", branch)
