#!/usr/bin/env python3
"""Run manual Web page/view-model profiling scenarios.

This is intentionally opt-in and excluded from default test/CI lanes. It
targets expensive client-side page assembly work for large hubs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_TEST = "src/lib/viewModels/pageViewModels.profile.test.ts"


def main() -> int:
    env = os.environ.copy()
    env["RUN_WEB_PAGE_PROFILE"] = "1"
    cmd = [
        "pnpm",
        "--filter",
        "@codex-autorunner/web-hub",
        "exec",
        "vitest",
        "run",
        PROFILE_TEST,
        "--reporter=verbose",
    ]
    return subprocess.call(cmd, cwd=REPO_ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
