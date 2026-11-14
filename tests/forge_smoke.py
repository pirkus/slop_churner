"""Offline smoke test for slop_churner.forge with stub gh/glab binaries.

The canonical suite now lives in tests/forge_test.py (the entry point the
harness runs). This file is kept as a thin compatibility wrapper so the
old `python3 tests/forge_smoke.py` invocation keeps working.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import forge_test  # noqa: E402

# Re-export the suite for backward compatibility.
make_stub = forge_test.make_stub
set_bin = forge_test.set_bin
clear_bin = forge_test.clear_bin
test_no_clis = forge_test.test_no_clis
test_gh_success = forge_test.test_gh_success
test_gh_failure_includes_tail = forge_test.test_gh_failure_includes_tail
test_glab_success = forge_test.test_glab_success
test_gh_preferred_over_glab = forge_test.test_gh_preferred_over_glab
test_timeout_never_raises = forge_test.test_timeout_never_raises
main = forge_test.main

if __name__ == "__main__":
    os.environ["PATH"] = forge_test.OLD_PATH
    forge_test.main()
    print("forge smoke: all tests passed")
