"""Adapter-layer error hierarchy for platform chat integrations.

This module is part of the adapter layer and composes shared core error types
so retry and severity behavior stays consistent across adapters.
"""

from __future__ import annotations

from typing import Optional

from ...core.exceptions import CodexError, PermanentError, TransientError


class ChatAdapterError(CodexError):
    """Base chat adapter error."""


class ChatAdapterTransientError(ChatAdapterError, TransientError):
    """Retryable adapter failure (network/rate-limit/transient backend state)."""


class ChatAdapterPermanentError(ChatAdapterError, PermanentError):
    """Non-retryable adapter failure (validation/auth/config)."""

    recoverable = PermanentError.recoverable
    severity = PermanentError.severity


class ChatAdapterTimeoutError(ChatAdapterTransientError):
    """Timeout while talking to a platform API."""

    def __init__(self, message: str, *, user_message: Optional[str] = None) -> None:
        if user_message is None:
            user_message = "Chat platform timed out. Retrying with backoff..."
        super().__init__(message, user_message=user_message)
