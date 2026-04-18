from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

from ....core.logging_utils import log_event
from ....integrations.app_server.env import app_server_env
from ..errors import DiscordAPIError
from ..rendering import truncate_for_discord

_logger = logging.getLogger(__name__)

SHELL_OUTPUT_TRUNCATION_SUFFIX = "\n...[truncated]..."


def _extract_command_result(
    result: subprocess.CompletedProcess[str],
) -> tuple[str, str, Optional[int]]:
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    exit_code = int(result.returncode) if isinstance(result.returncode, int) else 0
    return stdout, stderr, exit_code


def _format_shell_body(
    command: str, stdout: str, stderr: str, exit_code: Optional[int]
) -> str:
    lines = [f"$ {command}"]
    if stdout:
        lines.append(stdout.rstrip("\n"))
    if stderr:
        if stdout:
            lines.append("")
        lines.append("[stderr]")
        lines.append(stderr.rstrip("\n"))
    if not stdout and not stderr:
        lines.append("(no output)")
    if exit_code is not None and exit_code != 0:
        lines.append(f"(exit {exit_code})")
    return "\n".join(lines)


def _format_shell_message(body: str, *, note: Optional[str]) -> str:
    if note:
        return f"{note}\n```text\n{body}\n```"
    return f"```text\n{body}\n```"


def _prepare_shell_response(
    service: Any,
    full_body: str,
    *,
    filename: str,
) -> tuple[str, Optional[bytes]]:
    max_output_chars = max(1, int(service._config.shell.max_output_chars))
    max_message_length = max(64, int(service._config.max_message_length))

    message = _format_shell_message(full_body, note=None)
    if len(full_body) <= max_output_chars and len(message) <= max_message_length:
        return message, None

    note = f"Output too long; attached full output as {filename}. Showing head."
    head = full_body[:max_output_chars].rstrip()
    if len(head) < len(full_body):
        head = f"{head}{SHELL_OUTPUT_TRUNCATION_SUFFIX}"
    message = _format_shell_message(head, note=note)
    if len(message) > max_message_length:
        overhead = len(_format_shell_message("", note=note))
        allowed = max(
            0,
            max_message_length - overhead - len(SHELL_OUTPUT_TRUNCATION_SUFFIX),
        )
        head = full_body[:allowed].rstrip()
        if len(head) < len(full_body):
            head = f"{head}{SHELL_OUTPUT_TRUNCATION_SUFFIX}"
        message = _format_shell_message(head, note=note)
        if len(message) > max_message_length:
            message = truncate_for_discord(message, max_len=max_message_length)

    return message, full_body.encode("utf-8", errors="replace")


async def handle_bang_shell(
    service: Any,
    *,
    channel_id: str,
    message_id: str,
    text: str,
    workspace_root: Path,
) -> None:
    if not service._config.shell.enabled:
        await service._send_channel_message_safe(
            channel_id,
            {
                "content": (
                    "Shell commands are disabled. Enable `discord_bot.shell.enabled`."
                )
            },
            record_id=f"shell:{message_id}:disabled",
        )
        return

    command_text = text[1:].strip()
    if not command_text:
        await service._send_channel_message_safe(
            channel_id,
            {"content": "Prefix a command with `!` to run it locally. Example: `!ls`"},
            record_id=f"shell:{message_id}:usage",
        )
        return

    timeout_seconds = max(0.1, service._config.shell.timeout_ms / 1000.0)
    timeout_label = int(timeout_seconds + 0.999)
    shell_command = ["bash", "-lc", command_text]
    shell_env = app_server_env(shell_command, workspace_root)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            shell_command,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=shell_env,
        )
    except subprocess.TimeoutExpired:
        log_event(
            _logger,
            logging.WARNING,
            "discord.shell.timeout",
            channel_id=channel_id,
            command=command_text,
            timeout_seconds=timeout_seconds,
        )
        await service._send_channel_message_safe(
            channel_id,
            {
                "content": (
                    f"Shell command timed out after {timeout_label}s: `{command_text}`.\n"
                    "Interactive commands (top/htop/watch/tail -f) do not exit. "
                    "Try a one-shot flag like `top -l 1` (macOS) or `top -b -n 1` (Linux)."
                )
            },
            record_id=f"shell:{message_id}:timeout",
        )
        return
    except subprocess.SubprocessError as exc:
        log_event(
            _logger,
            logging.WARNING,
            "discord.shell.failed",
            channel_id=channel_id,
            command=command_text,
            workspace_root=str(workspace_root),
            exc=exc,
        )
        await service._send_channel_message_safe(
            channel_id,
            {"content": "Shell command failed; check logs for details."},
            record_id=f"shell:{message_id}:failed",
        )
        return

    stdout, stderr, exit_code = _extract_command_result(result)
    full_body = _format_shell_body(command_text, stdout, stderr, exit_code)
    filename = f"shell-output-{uuid.uuid4().hex[:8]}.txt"
    response_text, attachment = _prepare_shell_response(
        service,
        full_body,
        filename=filename,
    )
    await service._send_channel_message_safe(
        channel_id,
        {"content": response_text},
        record_id=f"shell:{message_id}:result",
    )
    if attachment is None:
        return
    try:
        await service._rest.create_channel_message_with_attachment(
            channel_id=channel_id,
            data=attachment,
            filename=filename,
        )
    except (DiscordAPIError, OSError) as exc:
        log_event(
            _logger,
            logging.WARNING,
            "discord.shell.attachment_failed",
            channel_id=channel_id,
            command=command_text,
            filename=filename,
            exc=exc,
        )
        await service._send_channel_message_safe(
            channel_id,
            {
                "content": "Failed to attach full shell output; showing truncated output."
            },
            record_id=f"shell:{message_id}:attachment_failed",
        )
