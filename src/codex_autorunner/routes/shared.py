"""
Shared utilities for route modules.
"""

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..about_car import ABOUT_CAR_REL_PATH, ensure_about_car_file
from ..state import load_state


def codex_supports_input_flag(binary: str) -> bool:
    """
    Best-effort capability check for `codex --input <file>` style context ingestion.

    Some Codex CLI versions support `--input` (often on `codex exec`); others don't.
    We probe `--help` so we can prefer `--input` when available without breaking older installs.
    """
    try:
        # Keep this fast and non-fatal; avoid hanging the server.
        result = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        return "--input" in text
    except Exception:
        return False


def build_codex_terminal_cmd(engine, *, resume_mode: bool) -> list[str]:
    """
    Build the subprocess argv for launching the Codex interactive CLI inside a PTY.

    For "new" sessions we seed a small CAR briefing as the initial prompt so Codex
    knows where canonical work docs live (even if `.codex-autorunner/` is gitignored).
    """
    if resume_mode:
        return [
            engine.config.codex_binary,
            "--yolo",
            "resume",
            *engine.config.codex_terminal_args,
        ]

    cmd = [
        engine.config.codex_binary,
        "--yolo",
        *engine.config.codex_terminal_args,
    ]
    try:
        about_path = engine.repo_root / ABOUT_CAR_REL_PATH
        if not about_path.exists():
            ensure_about_car_file(engine.config)
        if codex_supports_input_flag(engine.config.codex_binary):
            cmd.extend(["--input", str(about_path)])
        else:
            # Back-compat fallback: older Codex CLIs don't support --input.
            # In that case we still seed the session by passing the ABOUT text
            # as the initial prompt argument.
            about_text = about_path.read_text(encoding="utf-8")
            if about_text.strip():
                cmd.append(about_text)
    except Exception:
        # Best-effort: never block terminal launch due to context helper.
        pass
    return cmd


async def log_stream(log_path: Path):
    """SSE stream generator for log file tailing."""
    if not log_path.exists():
        yield "data: log file not found\n\n"
        return
    with log_path.open("r", encoding="utf-8") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                yield f"data: {line.rstrip()}\n\n"
            else:
                await asyncio.sleep(0.5)


async def state_stream(engine, manager, logger=None):
    """SSE stream generator for state updates."""
    last_payload = None
    last_error_log_at = 0.0
    terminal_idle_timeout_seconds = engine.config.terminal_idle_timeout_seconds
    while True:
        try:
            state = await asyncio.to_thread(load_state, engine.state_path)
            outstanding, done = await asyncio.to_thread(engine.docs.todos)
            payload = {
                "last_run_id": state.last_run_id,
                "status": state.status,
                "last_exit_code": state.last_exit_code,
                "last_run_started_at": state.last_run_started_at,
                "last_run_finished_at": state.last_run_finished_at,
                "outstanding_count": len(outstanding),
                "done_count": len(done),
                "running": manager.running,
                "runner_pid": state.runner_pid,
                "terminal_idle_timeout_seconds": terminal_idle_timeout_seconds,
            }
            if payload != last_payload:
                yield f"data: {json.dumps(payload)}\n\n"
                last_payload = payload
        except Exception:
            # Don't spam logs, but don't swallow silently either.
            now = time.time()
            if logger is not None and (now - last_error_log_at) > 60:
                last_error_log_at = now
                try:
                    logger.warning("state stream error", exc_info=True)
                except Exception:
                    pass
        await asyncio.sleep(1.0)
