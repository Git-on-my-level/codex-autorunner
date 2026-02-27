"""Shared utility functions for Telegram command handler modules."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx

from .....agents.opencode.client import OpenCodeProtocolError
from .....agents.opencode.supervisor import OpenCodeSupervisorError
from ...helpers import format_public_error
from ...payload_utils import extract_opencode_error_detail


def _format_exception_message(
    message: str,
    exc: Exception,
    *,
    use_parentheses: bool,
) -> Optional[str]:
    detail = str(exc).strip()
    if not detail:
        return None
    if use_parentheses:
        return f"{message} ({format_public_error(detail)})"
    return f"{message}: {format_public_error(detail)}"


def _build_opencode_exception_formatter(
    message: str,
    *,
    use_parentheses: bool,
) -> Callable[[Exception], str]:
    def _format(exc: Exception) -> str:
        detail_message = _format_exception_message(
            message,
            exc,
            use_parentheses=use_parentheses,
        )
        if detail_message is None:
            return f"{message}."
        if use_parentheses and not detail_message.endswith("."):
            return f"{detail_message}."
        return detail_message

    return _format


def _extract_error_detail_from_http_status(exc: httpx.HTTPStatusError) -> Optional[str]:
    try:
        payload = exc.response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        detail = extract_opencode_error_detail(payload)
        if isinstance(detail, str) and detail:
            return f"OpenCode error: {format_public_error(detail)}"
    response_text = exc.response.text.strip()
    if response_text:
        return f"OpenCode error: {format_public_error(response_text)}"
    return None


def _format_opencode_exception(exc: Exception) -> Optional[str]:
    for exc_type, formatter in (
        (
            OpenCodeSupervisorError,
            _build_opencode_exception_formatter(
                "OpenCode backend unavailable",
                use_parentheses=True,
            ),
        ),
        (
            OpenCodeProtocolError,
            _build_opencode_exception_formatter(
                "OpenCode protocol error",
                use_parentheses=False,
            ),
        ),
    ):
        if isinstance(exc, exc_type):
            return formatter(exc)
    if isinstance(exc, json.JSONDecodeError):
        return "OpenCode returned invalid JSON."
    if isinstance(exc, httpx.HTTPStatusError):
        detail = _extract_error_detail_from_http_status(exc)
        if detail is not None:
            return detail
        return f"OpenCode request failed (HTTP {exc.response.status_code})."
    if isinstance(exc, httpx.RequestError):
        return (
            _format_exception_message("OpenCode request failed", exc)
            or "OpenCode request failed."
        )
    return None


def _format_httpx_exception(exc: Exception) -> Optional[str]:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            detail = (
                payload.get("detail") or payload.get("message") or payload.get("error")
            )
            if isinstance(detail, str) and detail:
                return format_public_error(detail)
        response_text = exc.response.text.strip()
        if response_text:
            return format_public_error(response_text)
        return f"Request failed (HTTP {exc.response.status_code})."
    if isinstance(exc, httpx.RequestError):
        detail = str(exc).strip()
        if detail:
            return format_public_error(detail)
        return "Request failed."
    return None


def _opencode_review_arguments(target: dict[str, Any]) -> str:
    target_type = target.get("type")
    if target_type == "uncommittedChanges":
        return ""
    if target_type == "baseBranch":
        branch = target.get("branch")
        if isinstance(branch, str) and branch:
            return branch
    if target_type == "commit":
        sha = target.get("sha")
        if isinstance(sha, str) and sha:
            return sha
    if target_type == "custom":
        instructions = target.get("instructions")
        if isinstance(instructions, str):
            instructions = instructions.strip()
            if instructions:
                return f"uncommitted\n\n{instructions}"
        return "uncommitted"
    return json.dumps(target, sort_keys=True)
