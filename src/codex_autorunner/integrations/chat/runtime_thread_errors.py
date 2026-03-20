"""Shared runtime thread error sanitization for chat adapters.

This module provides a unified policy for sanitizing managed-thread turn
result errors before displaying them to users. Both Discord and Telegram
adapters should use this policy to ensure consistent behavior.

The policy:
- Timeout errors: return the surface-specific timeout message
- Interrupted errors: return the surface-specific interrupted message
- Empty/generic errors: return the surface-specific public error message
- Unknown errors: redact secrets and truncate, preserving safe detail

The rationale for preserving sanitized detail is that explicit backend
errors like "Auth required" are actionable by users, while secrets and
stack traces must be redacted.
"""

from __future__ import annotations

from typing import Any

from ...core.orchestration.runtime_threads import (
    RUNTIME_THREAD_INTERRUPTED_ERROR,
    RUNTIME_THREAD_TIMEOUT_ERROR,
)
from ...core.redaction import redact_text

DEFAULT_ERROR_DETAIL_LIMIT = 200


def sanitize_runtime_thread_error(
    detail: Any,
    *,
    public_error: str,
    timeout_error: str,
    interrupted_error: str,
    detail_limit: int = DEFAULT_ERROR_DETAIL_LIMIT,
) -> str:
    """Sanitize a managed-thread turn result error for user display.

    Args:
        detail: The raw error detail (may be any type).
        public_error: Generic error message for this surface (e.g., "Discord turn failed").
        timeout_error: Timeout message for this surface.
        interrupted_error: Interrupted message for this surface.
        detail_limit: Maximum length for sanitized detail (default 200).

    Returns:
        Sanitized error string safe for user display.
    """
    sanitized = str(detail or "").strip()

    if sanitized in {RUNTIME_THREAD_TIMEOUT_ERROR, timeout_error}:
        return timeout_error
    if sanitized in {RUNTIME_THREAD_INTERRUPTED_ERROR, interrupted_error}:
        return interrupted_error
    if sanitized in {timeout_error, interrupted_error}:
        return sanitized

    if not sanitized or sanitized in {public_error, "Runtime thread failed"}:
        return public_error

    return format_public_error(sanitized, limit=detail_limit)


def resolve_runtime_thread_error_detail(
    *,
    execution_error: Any = None,
    outcome_error: Any = None,
    event_error: Any = None,
    public_error: str,
    timeout_error: str,
    interrupted_error: str,
    detail_limit: int = DEFAULT_ERROR_DETAIL_LIMIT,
) -> str:
    """Pick the best available runtime-thread error detail and sanitize it."""

    raw_detail = next(
        (
            candidate
            for candidate in (execution_error, outcome_error, event_error)
            if str(candidate or "").strip()
        ),
        None,
    )
    return sanitize_runtime_thread_error(
        raw_detail,
        public_error=public_error,
        timeout_error=timeout_error,
        interrupted_error=interrupted_error,
        detail_limit=detail_limit,
    )


def format_public_error(detail: str, *, limit: int = DEFAULT_ERROR_DETAIL_LIMIT) -> str:
    """Format error detail for public messages with redaction and truncation.

    This helper ensures all user-visible error text is:
    - Short and readable
    - Redacted for known secret patterns
    - Does not include raw file contents or stack traces

    Args:
        detail: Error detail string to format.
        limit: Maximum length of output (default 200).

    Returns:
        Formatted error string with secrets redacted and length limited.
    """
    normalized = " ".join(detail.split())
    redacted = redact_text(normalized)
    if len(redacted) > limit:
        return f"{redacted[: limit - 3]}..."
    return redacted
