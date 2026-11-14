"""Offline test for slop_churner.forge with stub gh/glab binaries.

Run:  python3 tests/forge_test.py
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import slop_churner.forge as forge  # noqa: E402

OLD_PATH = os.environ.get("PATH", "")


def make_stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXUSR)


def set_bin(bin_dir: Path) -> None:
    os.environ["PATH"] = str(bin_dir)


def make_repo(root: Path) -> None:
    """Throwaway git repo to pass as the `repo` arg of create_pr."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "slop-churner@test"],
                   cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "slop-churner"],
                   cwd=str(root), check=True, capture_output=True)
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True,
                   capture_output=True)


def clear_bin() -> None:
    os.environ["PATH"] = "/usr/bin:/bin"


def test_no_clis(tmp: Path) -> None:
    """check_forge and create_pr must not raise when no gh/glab exist."""
    empty = tmp / "empty"
    empty.mkdir()
    set_bin(empty)
    infos = forge.check_forge()
    assert [i.name for i in infos] == ["gh", "glab"]
    for info in infos:
        assert not info.available and not info.authenticated
        assert info.detail == "not on PATH"
    r = forge.create_pr(tmp, "main", "feature", "t", "b")
    assert not r.ok and r.url == ""
    assert "no gh/glab CLI found on PATH" in r.error


def test_gh_success(tmp: Path) -> None:
    b = tmp / "gh_ok"
    repo = tmp / "repo_gh_ok"
    b.mkdir()
    make_stub(b, "gh", """
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
  echo "Logged in to github.com account (token gho_testtoken123)"; exit 0; fi
echo "Created pull request"
echo "https://github.com/x/y/pull/42"
exit 0
""")
    make_repo(repo)
    set_bin(b)
    infos = forge.check_forge()
    gh = infos[0]
    assert gh.name == "gh" and gh.available and gh.authenticated
    glab = infos[1]
    assert glab.name == "glab" and not glab.available

    r = forge.create_pr(repo, "main", "feature", "My title", "My body")
    assert r.ok, r.error
    assert r.url == "https://github.com/x/y/pull/42"
    assert "https://github.com/x/y/pull/42" in r.output
    assert r.error == ""


def test_gh_failure_includes_tail(tmp: Path) -> None:
    b = tmp / "gh_fail"
    repo = tmp / "repo_gh_fail"
    b.mkdir()
    make_stub(b, "gh", """
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
  echo "github.com: Not logged in" >&2; exit 1; fi
echo "no remote named 'origin'" >&2
exit 1
""")
    set_bin(b)
    infos = forge.check_forge()
    assert infos[0].available and not infos[0].authenticated
    assert "auth status failed" in infos[0].detail

    repo.mkdir()
    r = forge.create_pr(repo, "main", "feature", "t", "b")
    assert not r.ok and r.url == ""
    assert "no remote named 'origin'" in r.error


def test_glab_success(tmp: Path) -> None:
    b = tmp / "glab_ok"
    repo = tmp / "repo_glab_ok"
    b.mkdir()
    make_stub(b, "glab", """
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then exit 0; fi
echo "Creating merge request for feature into main"
echo "Opened merge request !7"
echo "https://gitlab.com/example/repo/-/merge_requests/7"
exit 0
""")
    set_bin(b)
    infos = forge.check_forge()
    assert not infos[0].available          # no gh
    assert infos[1].available and infos[1].authenticated

    repo.mkdir()
    r = forge.create_pr(repo, "main", "feature", "t", "b")
    assert r.ok, r.error
    assert r.url == "https://gitlab.com/example/repo/-/merge_requests/7"


def test_gh_preferred_over_glab(tmp: Path) -> None:
    b = tmp / "both"
    b.mkdir()
    make_stub(b, "gh", "echo https://github.com/example/repo/pull/1\n")
    make_stub(b, "glab", "echo https://gitlab.com/example/repo/-/merge_requests/2\n")
    set_bin(b)
    (repo := tmp / "repo_both").mkdir()
    r = forge.create_pr(repo, "main", "feature", "t", "b")
    assert r.ok and r.url.startswith("https://github.com/"), r


def test_timeout_never_raises(tmp: Path) -> None:
    b = tmp / "slow"
    b.mkdir()
    make_stub(b, "gh", "while :; do :; done\n")
    set_bin(b)
    old_auth, old_pr = forge.AUTH_TIMEOUT, forge.PR_TIMEOUT
    forge.AUTH_TIMEOUT = 1
    forge.PR_TIMEOUT = 1
    try:
        info = forge.check_forge()[0]
        assert info.available and not info.authenticated
        r = forge.create_pr(tmp, "main", "feature", "t", "b")
        assert not r.ok and "timed out" in r.error
    finally:
        forge.AUTH_TIMEOUT, forge.PR_TIMEOUT = old_auth, old_pr


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for fn in (test_no_clis, test_gh_success, test_gh_failure_includes_tail,
                   test_glab_success, test_gh_preferred_over_glab, test_timeout_never_raises):
            fn(tmp)
            os.environ["PATH"] = OLD_PATH  # restore so later tests find git etc.
            print(f"ok: {fn.__name__}")
    print("forge test: all tests passed")


if __name__ == "__main__":
    try:
        main()
    finally:
        os.environ["PATH"] = OLD_PATH
