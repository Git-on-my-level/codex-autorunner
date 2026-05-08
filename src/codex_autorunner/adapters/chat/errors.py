"""Adapter-layer error hierarchy for platform chat integrations.

This module is part of the adapter layer and composes shared core error types
so retry and severity behavior stays consistent across adapters.
"""

from __future__ import annotations

from ...core.exceptions import CodexError, PermanentError


class ChatAdapterError(CodexError):
    """Base chat adapter error."""


class ChatAdapterPermanentError(ChatAdapterError, PermanentError):
    """Non-retryable adapter failure (validation/auth/config)."""

    recoverable = PermanentError.recoverable
    severity = PermanentError.severity
