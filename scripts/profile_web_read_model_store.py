#!/usr/bin/env python3
"""Run the manual Web read-model store profiling suite.

This intentionally stays out of default test and CI lanes. It exercises large
cached chat/transcript states so agents can isolate browser-side store update
costs without needing a live hub.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_TEST = "src/lib/data/readModelStore.profile.test.ts"


def main() -> int:
    env = os.environ.copy()
    env["RUN_WEB_STORE_PROFILE"] = "1"
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
