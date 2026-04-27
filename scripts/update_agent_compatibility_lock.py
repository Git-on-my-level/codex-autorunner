#!/usr/bin/env python3

"""Update the pinned agent compatibility tool versions."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.protocol_utils import validate_binary_path

VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")


def _read_version(binary_name: str, env_var: str) -> str:
    path = validate_binary_path(binary_name, env_var)
    result = subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    match = VERSION_RE.search(result.stdout.strip()) or VERSION_RE.search(
        result.stderr.strip()
    )
    if not match:
        raise RuntimeError(
            f"Could not parse {binary_name} version from {result.stdout}"
        )
    return match.group(1)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    output_path = repo_root / "vendor" / "protocols" / "agent-compatibility.lock.json"
    lock = {
        "codex": _read_version("codex", "CODEX_BIN"),
        "opencode": _read_version("opencode", "OPENCODE_BIN"),
    }
    output_path.write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Updated {output_path.relative_to(repo_root)}")
    print(f"  Codex: {lock['codex']}")
    print(f"  OpenCode: {lock['opencode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
