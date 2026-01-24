# DEPRECATED: This module implements a Codex CLI subprocess runner.
# The primary execution path now uses the Codex app-server via OpenCode runtime.
# This file is kept for potential future CLI-as-backend support but is currently
# not referenced by the main engine. See src/codex_autorunner/core/engine.py for
# the current execution path (_run_codex_app_server_async).

import asyncio
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .config import Config, ConfigError
from .utils import (
    apply_codex_options,
    resolve_executable,
    subprocess_env,
    supports_reasoning,
)


class CodexRunnerError(Exception):
    """Raised when a Codex subprocess fails at the runner boundary."""


class CodexTimeoutError(CodexRunnerError):
    """Raised when a Codex subprocess exceeds the timeout."""


def resolve_codex_binary(config: Config) -> str:
    resolved = resolve_executable(config.codex_binary)
    if not resolved:
        raise ConfigError(f"Codex binary not found: {config.codex_binary}")
    return resolved


def build_codex_command(
    config: Config, prompt: str, *, resolved_binary: Optional[str] = None
) -> list[str]:
    binary = resolved_binary or resolve_codex_binary(config)
    reasoning_supported = supports_reasoning(binary)
    args = apply_codex_options(
        config.codex_args,
        model=config.codex_model,
        reasoning=config.codex_reasoning,
        supports_reasoning=reasoning_supported,
    )
    return [binary] + args + [prompt]


def run_codex_streaming(
    config: Config,
    repo_root: Path,
    prompt: str,
    *,
    on_stdout_line: Optional[Callable[[str], None]] = None,
    cmd: Optional[list[str]] = None,
) -> int:
    cmd = cmd or build_codex_command(config, prompt)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            env=subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise ConfigError(f"Codex binary not found: {config.codex_binary}") from exc

    if proc.stdout:
        for line in proc.stdout:
            if on_stdout_line:
                on_stdout_line(line.rstrip("\n"))

    return proc.wait()


async def run_codex_capture_async(
    config: Config,
    repo_root: Path,
    prompt: str,
    *,
    timeout_seconds: Optional[int] = None,
    cmd: Optional[list[str]] = None,
) -> tuple[int, str]:
    cmd = cmd or build_codex_command(config, prompt)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise ConfigError(f"Codex binary not found: {config.codex_binary}") from exc

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise CodexTimeoutError("Codex process timed out") from exc

    output = stdout.decode("utf-8", errors="ignore") if stdout else ""
    returncode = proc.returncode
    if returncode is None:
        returncode = await proc.wait()
    return returncode, output
