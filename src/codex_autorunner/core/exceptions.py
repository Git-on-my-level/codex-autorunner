from __future__ import annotations

from enum import Enum
from typing import Optional


class Severity(str, Enum):
    """Error severity levels."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class CodexError(Exception):
    """Base exception for all codex-autorunner errors."""

    severity: Severity = Severity.WARNING
    recoverable: bool = True
    user_message: Optional[str] = None

    def __init__(self, message: str, *, user_message: Optional[str] = None) -> None:
        super().__init__(message)
        if user_message is not None:
            self.user_message = user_message


class TransientError(CodexError):
    """Retryable errors (network, rate limits, temporary failures)."""

    recoverable = True
    severity = Severity.WARNING


class PermanentError(CodexError):
    """Non-retryable errors (config, auth, validation)."""

    recoverable = False
    severity = Severity.ERROR


class CriticalError(CodexError):
    """System-level failures requiring intervention."""

    severity = Severity.CRITICAL
    recoverable = False


class CircuitOpenError(CriticalError):
    """Raised when a circuit breaker is open."""

    def __init__(self, service_name: str, message: Optional[str] = None) -> None:
        self.service_name = service_name
        msg = message or f"Circuit breaker is open for {service_name}"
        super().__init__(
            msg,
            user_message=f"{service_name} is temporarily unavailable. Please try again later.",
        )


class AppServerError(CodexError):
    pass
