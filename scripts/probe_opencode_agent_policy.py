#!/usr/bin/env python3
"""Probe local OpenCode agent permission behavior used by CAR.

This script uses CAR's generator, then verifies the result with the real
``opencode agent list`` command. It checks that the installed OpenCode build
parses CAR-style agent frontmatter into the permission rules CAR relies on for
read-only Task subagents.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _write_agent(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_opencode_agent_list(workspace: Path) -> str:
    opencode = shutil.which("opencode")
    if opencode is None:
        raise RuntimeError("opencode binary not found on PATH")
    result = subprocess.run(
        [opencode, "agent", "list"],
        cwd=workspace,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _require(output: str, needle: str) -> None:
    if needle not in output:
        raise AssertionError(f"expected OpenCode agent list output to contain: {needle}")


async def probe(workspace: Path) -> None:
    from codex_autorunner.agents.opencode.agent_config import ensure_agent_config

    agent_dir = workspace / ".opencode" / "agent"
    await ensure_agent_config(
        workspace_root=workspace,
        agent_id="car-review-coordinator",
        model="zai-coding-plan/glm-5.1",
        title="CAR review coordinator",
        description="CAR OpenCode review coordinator",
        mode="primary",
        permission={"task": {"*": "deny", "car-read-explore": "allow"}},
        body="Coordinate review work. Use Task only for read-only review subagents.\n",
    )
    await ensure_agent_config(
        workspace_root=workspace,
        agent_id="car-read-explore",
        model="zai-coding-plan/glm-5.1",
        title="CAR read-only review explorer",
        description="Read-only CAR review subagent",
        mode="subagent",
        permission={
            "edit": "deny",
            "write": "deny",
            "bash": "deny",
            "todowrite": "deny",
        },
        body="Read repository files and return findings to the coordinator.\n",
    )
    _write_agent(
        agent_dir / "car-write-helper.md",
        """---
agent: car-write-helper
title: "CAR write helper"
description: "Write-capable helper that should not be task-allowlisted"
model: zai-coding-plan/glm-5.1
mode: subagent
permission:
  "*": allow
---
Write-capable helper.
""",
    )

    output = _run_opencode_agent_list(workspace)
    for needle in (
        "car-review-coordinator (primary)",
        '"permission": "task"',
        '"pattern": "*"',
        '"action": "deny"',
        '"pattern": "car-read-explore"',
        '"action": "allow"',
        "car-read-explore (subagent)",
        '"permission": "edit"',
        '"permission": "write"',
        '"permission": "bash"',
        '"permission": "todowrite"',
        "car-write-helper (subagent)",
    ):
        _require(output, needle)
    if '"pattern": "car-write-helper"' in output:
        raise AssertionError("write helper was unexpectedly allowlisted for task")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the temporary workspace for inspection",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        asyncio.run(probe(workspace))
        if args.keep:
            kept = Path(tempfile.mkdtemp(prefix="car-opencode-agent-policy-"))
            shutil.copytree(workspace, kept, dirs_exist_ok=True)
            print(f"kept probe workspace: {kept}")
    print("local OpenCode agent policy probe passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
