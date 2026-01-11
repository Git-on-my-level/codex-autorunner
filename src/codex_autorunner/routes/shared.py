"""
Shared utilities for route modules.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from ..codex_cli import extract_flag_value
from ..core.locks import process_alive, read_lock_info
from ..core.state import load_state

BYPASS_FLAGS = {
    "--yolo",
    "--dangerously-bypass-approvals-and-sandbox",
}


def _extract_bypass_flag(args: list[str]) -> tuple[str, list[str]]:
    chosen = None
    for arg in args:
        if arg in BYPASS_FLAGS:
            chosen = arg
            break
    filtered = [arg for arg in args if arg not in BYPASS_FLAGS]
    return chosen or "--yolo", filtered


def build_codex_terminal_cmd(engine, *, resume_mode: bool) -> list[str]:
    """
    Build the subprocess argv for launching the Codex interactive CLI inside a PTY.
    """
    bypass_flag, terminal_args = _extract_bypass_flag(
        list(engine.config.codex_terminal_args)
    )
    if resume_mode:
        return [
            engine.config.codex_binary,
            bypass_flag,
            "resume",
            *terminal_args,
        ]

    cmd = [
        engine.config.codex_binary,
        bypass_flag,
        *terminal_args,
    ]
    return cmd


def resolve_runner_status(engine, state) -> tuple[str, Optional[int], bool]:
    pid = state.runner_pid
    alive_pid = pid if pid and process_alive(pid) else None
    if alive_pid is None:
        info = read_lock_info(engine.lock_path)
        if info.pid and process_alive(info.pid):
            alive_pid = info.pid
    running = alive_pid is not None
    status = state.status
    if status == "running" and not running:
        status = "idle"
    runner_pid = alive_pid if running else None
    return status, runner_pid, running


async def log_stream(log_path: Path, heartbeat_interval: float = 15.0):
    """SSE stream generator for log file tailing."""
    if not log_path.exists():
        yield "data: log file not found\n\n"
        return
    last_emit_at = time.monotonic()
    with log_path.open("r", encoding="utf-8") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                yield f"data: {line.rstrip()}\n\n"
                last_emit_at = time.monotonic()
            else:
                now = time.monotonic()
                if now - last_emit_at >= heartbeat_interval:
                    yield ": ping\n\n"
                    last_emit_at = now
                await asyncio.sleep(0.5)


async def state_stream(engine, manager, logger=None, heartbeat_interval: float = 15.0):
    """SSE stream generator for state updates."""
    last_payload = None
    last_error_log_at = 0.0
    last_emit_at = time.monotonic()
    terminal_idle_timeout_seconds = engine.config.terminal_idle_timeout_seconds
    codex_model = engine.config.codex_model or extract_flag_value(
        engine.config.codex_args, "--model"
    )
    while True:
        emitted = False
        try:
            state = await asyncio.to_thread(load_state, engine.state_path)
            outstanding, done = await asyncio.to_thread(engine.docs.todos)
            status, runner_pid, running = resolve_runner_status(engine, state)
            payload = {
                "last_run_id": state.last_run_id,
                "status": status,
                "last_exit_code": state.last_exit_code,
                "last_run_started_at": state.last_run_started_at,
                "last_run_finished_at": state.last_run_finished_at,
                "outstanding_count": len(outstanding),
                "done_count": len(done),
                "running": running,
                "runner_pid": runner_pid,
                "terminal_idle_timeout_seconds": terminal_idle_timeout_seconds,
                "codex_model": codex_model or "auto",
            }
            if payload != last_payload:
                yield f"data: {json.dumps(payload)}\n\n"
                last_payload = payload
                last_emit_at = time.monotonic()
                emitted = True
        except Exception:
            # Don't spam logs, but don't swallow silently either.
            now = time.time()
            if logger is not None and (now - last_error_log_at) > 60:
                last_error_log_at = now
                try:
                    logger.warning("state stream error", exc_info=True)
                except Exception:
                    pass
        if not emitted:
            now = time.monotonic()
            if now - last_emit_at >= heartbeat_interval:
                yield ": ping\n\n"
                last_emit_at = now
        await asyncio.sleep(1.0)
