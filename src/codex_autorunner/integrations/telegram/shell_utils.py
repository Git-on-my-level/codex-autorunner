from typing import Any, Optional

from .constants import SHELL_OUTPUT_TRUNCATION_SUFFIX, TELEGRAM_MAX_MESSAGE_LENGTH


def _render_command_output(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        stdout = result.get("stdout") or result.get("stdOut") or result.get("output")
        stderr = result.get("stderr") or result.get("stdErr")
        if isinstance(stdout, str) and isinstance(stderr, str):
            if stdout and stderr:
                return stdout.rstrip("\n") + "\n" + stderr
            if stdout:
                return stdout
            return stderr
        if isinstance(stdout, str):
            return stdout
        if isinstance(stderr, str):
            return stderr
    return ""


def _extract_command_result(result: Any) -> tuple[str, str, Optional[int]]:
    stdout = ""
    stderr = ""
    exit_code = None
    if isinstance(result, str):
        stdout = result
        return stdout, stderr, exit_code
    if isinstance(result, dict):
        stdout_value = (
            result.get("stdout") or result.get("stdOut") or result.get("output")
        )
        stderr_value = result.get("stderr") or result.get("stdErr")
        exit_value = result.get("exitCode") or result.get("exit_code")
        if isinstance(stdout_value, str):
            stdout = stdout_value
        if isinstance(stderr_value, str):
            stderr = stderr_value
        if isinstance(exit_value, int):
            exit_code = exit_value
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
    full_body: str,
    *,
    max_output_chars: int,
    filename: str,
) -> tuple[str, Optional[bytes]]:
    message = _format_shell_message(full_body, note=None)
    if (
        len(full_body) <= max_output_chars
        and len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH
    ):
        return message, None
    note = f"Output too long; attached full output as {filename}. Showing head."
    limit = max_output_chars
    head = full_body[:limit].rstrip()
    head = f"{head}{SHELL_OUTPUT_TRUNCATION_SUFFIX}"
    message = _format_shell_message(head, note=note)
    if len(message) > TELEGRAM_MAX_MESSAGE_LENGTH:
        excess = len(message) - TELEGRAM_MAX_MESSAGE_LENGTH
        allowed = max(0, limit - excess)
        head = full_body[:allowed].rstrip()
        head = f"{head}{SHELL_OUTPUT_TRUNCATION_SUFFIX}"
        message = _format_shell_message(head, note=note)
    attachment = full_body.encode("utf-8", errors="replace")
    return message, attachment


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data


def _extract_command_text(item: dict[str, Any], params: dict[str, Any]) -> str:
    command = item.get("command") if isinstance(item, dict) else None
    if command is None and isinstance(params, dict):
        command = params.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command).strip()
    if isinstance(command, str):
        return command.strip()
    return ""
