#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath

PROTECTED_PREFIX = ".codex-autorunner"
ALLOWED_PATHS = {".codex-autorunner/.gitignore"}


def _normalize_path(raw: str) -> str:
    return PurePosixPath(raw.strip()).as_posix()


def _staged_paths() -> tuple[int, str, str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _blocked_paths(paths: list[str]) -> list[str]:
    blocked: list[str] = []
    for raw_path in paths:
        normalized = _normalize_path(raw_path)
        if not normalized:
            continue
        if normalized in ALLOWED_PATHS:
            continue
        if normalized == PROTECTED_PREFIX or normalized.startswith(
            f"{PROTECTED_PREFIX}/"
        ):
            blocked.append(normalized)
    return blocked


def main() -> int:
    code, stdout, stderr = _staged_paths()
    if code != 0:
        message = stderr.strip() or "failed to inspect staged files"
        print(f"Unable to inspect staged files: {message}", file=sys.stderr)
        return code

    blocked = _blocked_paths(stdout.splitlines())
    if not blocked:
        print("No staged .codex-autorunner paths found.")
        return 0

    print(
        "Refusing to commit staged files under .codex-autorunner/ "
        "(except .codex-autorunner/.gitignore).",
        file=sys.stderr,
    )
    for path in blocked:
        print(f" - {path}", file=sys.stderr)
    print(
        "Move durable source files out of .codex-autorunner/ or unstage them before "
        "committing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
