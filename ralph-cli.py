#!/usr/bin/env python3
"""Standalone launcher: `python3 /path/to/ralph/ralph-cli.py <command> ...`

Works without installing the package or setting PYTHONPATH, because it puts
the project root on sys.path and delegates to the ralph package.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ralph.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
