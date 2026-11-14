from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

AUTH_TIMEOUT = 10   # seconds for `auth status` probes
PR_TIMEOUT = 60     # seconds for `pr/mr create`

_URL_RE = re.compile(r"https?://\S+")


@dataclass
class ForgeInfo:
    name: str          # 'gh' or 'glab'
    available: bool    # binary on PATH
    authenticated: bool
    detail: str        # human-readable reason, e.g. 'not on PATH', 'gh auth status failed (no token?)'


@dataclass
class PRResult:
    ok: bool
    url: str
    output: str
    error: str


def _probe(name: str) -> ForgeInfo:
    """Check one CLI: on PATH? authenticated? Never raises."""
    if shutil.which(name) is None:
        return ForgeInfo(name=name, available=False, authenticated=False,
                         detail="not on PATH")
    try:
        proc = subprocess.run(
            [name, "auth", "status"],
            capture_output=True, text=True, timeout=AUTH_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ForgeInfo(name=name, available=True, authenticated=False,
                         detail=f"{name} auth status timed out or failed")
    if proc.returncode == 0:
        return ForgeInfo(name=name, available=True, authenticated=True, detail="ok")
    return ForgeInfo(name=name, available=True, authenticated=False,
                     detail=f"{name} auth status failed (no token?)")


def check_forge() -> list[ForgeInfo]:
    """Probe 'gh' first, then 'glab'. Never raises."""
    return [_probe(name) for name in ("gh", "glab")]


def _pick_cli() -> str | None:
    """Choose the best CLI: authenticated first, then just available. None if no gh/glab."""
    infos = check_forge()
    for want_auth in (True, False):
        for info in infos:  # 'gh' comes first, so it wins ties
            if info.available and info.authenticated == want_auth:
                return info.name
    return None


def _tail(text: str, lines: int = 5) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def create_pr(repo: Path, base: str, head: str, title: str, body: str) -> PRResult:
    """Create a PR (gh) or MR (glab) in `repo`. Never raises."""
    cli = _pick_cli()
    if cli is None:
        return PRResult(ok=False, url="", output="",
                        error="no gh/glab CLI found on PATH")
    if cli == "gh":
        cmd = ["gh", "pr", "create",
               "--base", base, "--head", head, "--title", title, "--body", body]
    else:
        cmd = ["glab", "mr", "create",
               "--base-branch", base, "--source-branch", head,
               "--title", title, "--description", body, "--yes"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo), capture_output=True, text=True, timeout=PR_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return PRResult(ok=False, url="", output="",
                        error=f"{cli} timed out after {PR_TIMEOUT}s")
    except OSError as e:
        return PRResult(ok=False, url="", output="",
                        error=f"{cli} failed to start: {e}")

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        detail = _tail(output) or "no output"
        return PRResult(ok=False, url="", output=output,
                        error=f"{cli} failed (exit {proc.returncode}): {detail}")
    urls = _URL_RE.findall(proc.stdout or "")
    if not urls:
        detail = _tail(output) or "no output"
        return PRResult(ok=False, url="", output=output,
                        error=f"no PR/MR URL found in {cli} output: {detail}")
    return PRResult(ok=True, url=urls[-1], output=output, error="")
