from __future__ import annotations

from typing import Any, Optional


class ACPError(RuntimeError):
    """Base error for ACP transport and protocol failures."""


class ACPProtocolError(ACPError):
    """Raised when the ACP subprocess emits malformed protocol frames."""


class ACPTransportError(ACPError):
    """Raised when the ACP subprocess transport is unavailable."""


class ACPResponseError(ACPError):
    """Raised when the ACP server returns an error response."""

    def __init__(
        self,
        *,
        method: Optional[str],
        code: Optional[int],
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.code = code
        self.data = data


class ACPMethodNotFoundError(ACPResponseError):
    """Raised when the ACP server does not implement a requested method."""


class ACPInitializationError(ACPError):
    """Raised when the ACP initialize handshake fails."""


class ACPProcessCrashedError(ACPTransportError):
    """Raised when the ACP subprocess exits unexpectedly."""

    def __init__(
        self,
        message: str,
        *,
        returncode: Optional[int] = None,
        stderr_tail: Optional[tuple[str, ...]] = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail or ()


__all__ = [
    "ACPError",
    "ACPInitializationError",
    "ACPMethodNotFoundError",
    "ACPProcessCrashedError",
    "ACPProtocolError",
    "ACPResponseError",
    "ACPTransportError",
]
