from ...core.exceptions import (
    AppServerError,
    PermanentError,
    TransientError,
)


class CodexAppServerError(AppServerError):
    """Base error for app-server client failures."""


class CodexAppServerResponseError(CodexAppServerError):
    """Raised when the app-server responds with an error payload."""

    def __init__(
        self,
        *,
        method=None,
        code=None,
        message="",
        data=None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.code = code
        self.data = data


class CodexAppServerDisconnected(CodexAppServerError, TransientError):
    """Raised when the app-server disconnects mid-flight."""

    def __init__(self, message: str = "App-server disconnected") -> None:
        super().__init__(
            message, user_message="App-server temporarily unavailable. Reconnecting..."
        )


class CodexAppServerProtocolError(CodexAppServerError, PermanentError):
    """Raised when the app-server returns malformed responses."""

    def __init__(self, message: str) -> None:
        super().__init__(message, user_message="App-server protocol error. Check logs.")


_MISSING_THREAD_MARKERS = (
    "thread not found",
    "no rollout found for thread id",
)


def is_missing_thread_error(exc: Exception) -> bool:
    if not isinstance(exc, CodexAppServerResponseError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _MISSING_THREAD_MARKERS)


__all__ = [
    "CodexAppServerError",
    "CodexAppServerDisconnected",
    "CodexAppServerProtocolError",
    "CodexAppServerResponseError",
    "is_missing_thread_error",
]
