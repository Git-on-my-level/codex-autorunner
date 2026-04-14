from __future__ import annotations

from typing import Optional

from ...core.exceptions import PermanentError, TransientError


class DiscordError(Exception):
    """Base Discord integration error."""

    def __init__(self, message: str, *, user_message: Optional[str] = None) -> None:
        super().__init__(message)
        self.user_message = user_message


class DiscordAPIError(DiscordError):
    """Discord API request error."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: Optional[int] = None,
        user_message: Optional[str] = None,
    ) -> None:
        if user_message is None:
            user_message = "Discord API error. Retrying with backoff..."
        super().__init__(message, user_message=user_message)
        self.retry_after = retry_after


class DiscordTransientError(DiscordAPIError, TransientError):
    """Retryable Discord API error (rate limits, network issues)."""


class DiscordPermanentError(DiscordAPIError, PermanentError):
    """Non-retryable Discord API error (config errors, invalid requests)."""

    recoverable = PermanentError.recoverable
    severity = PermanentError.severity


def is_unknown_interaction_error(error: BaseException | str | None) -> bool:
    if error is None:
        return False
    normalized = str(error).lower()
    return "unknown interaction" in normalized or "10062" in normalized
