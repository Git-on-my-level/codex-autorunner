"""Shared helper methods for Telegram command handlers."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .....agents.opencode.client import OpenCodeProtocolError
from .....agents.opencode.supervisor import OpenCodeSupervisorError
from ...adapter import InlineButton, build_inline_keyboard, encode_cancel_callback
from ...helpers import format_public_error

if TYPE_CHECKING:
    pass


class SharedHelpers:
    """Shared helper methods for Telegram command handlers.

    This class is designed to be used as a mixin in command handler classes.
    All methods use `self` to access instance attributes.
    """

    def _coerce_int(self, value: Any) -> Optional[int]:
        """Safely coerce value to int, rejecting bool.

        Args:
            value: Value to coerce to int.

        Returns:
            Integer value if coercion succeeds and value is not bool, None otherwise.
        """
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except Exception:
            return None

    def _format_httpx_exception(self, exc: Exception) -> Optional[str]:
        """Format httpx exceptions for user-friendly error messages.

        Args:
            exc: Exception to format.

        Returns:
            Formatted error message string or None if exception is not an httpx error.
        """
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                payload = exc.response.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                detail = (
                    payload.get("detail")
                    or payload.get("message")
                    or payload.get("error")
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

    def _format_opencode_exception(self, exc: Exception) -> Optional[str]:
        """Format OpenCode exceptions for user-friendly error messages.

        Args:
            exc: Exception to format.

        Returns:
            Formatted error message string or None if exception is not recognized.
        """
        if isinstance(exc, OpenCodeSupervisorError):
            detail = str(exc).strip()
            if detail:
                return f"OpenCode backend unavailable ({format_public_error(detail)})."
            return "OpenCode backend unavailable."
        if isinstance(exc, OpenCodeProtocolError):
            detail = str(exc).strip()
            if detail:
                return f"OpenCode protocol error: {format_public_error(detail)}"
            return "OpenCode protocol error."
        if isinstance(exc, json.JSONDecodeError):
            return "OpenCode returned invalid JSON."
        if isinstance(exc, httpx.HTTPStatusError):
            detail = None
            try:
                detail = self._extract_opencode_error_detail(exc.response.json())
            except Exception:
                detail = None
            if detail:
                return f"OpenCode error: {format_public_error(detail)}"
            response_text = exc.response.text.strip()
            if response_text:
                return f"OpenCode error: {format_public_error(response_text)}"
            return f"OpenCode request failed (HTTP {exc.response.status_code})."
        if isinstance(exc, httpx.RequestError):
            detail = str(exc).strip()
            if detail:
                return f"OpenCode request failed: {format_public_error(detail)}"
            return "OpenCode request failed."
        return None

    def _extract_opencode_error_detail(self, payload: Any) -> Optional[str]:
        """Extract error detail from OpenCode response payload.

        Args:
            payload: Response payload to extract error detail from.

        Returns:
            Error detail string if found, None otherwise.
        """
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "error", "reason"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(error, str) and error:
            return error
        for key in ("detail", "message", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _extract_opencode_session_path(self, payload: Any) -> Optional[str]:
        """Extract session path from OpenCode payload.

        Args:
            payload: Payload to extract session path from.

        Returns:
            Session path string if found, None otherwise.
        """
        if not isinstance(payload, dict):
            return None
        for key in ("directory", "path", "workspace_path", "workspacePath"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        properties = payload.get("properties")
        if isinstance(properties, dict):
            for key in ("directory", "path", "workspace_path", "workspacePath"):
                value = properties.get(key)
                if isinstance(value, str) and value:
                    return value
        session = payload.get("session")
        if isinstance(session, dict):
            return self._extract_opencode_session_path(session)
        return None

    def _interrupt_keyboard(self) -> dict[str, Any]:
        """Build interrupt button keyboard.

        Returns:
            Inline keyboard with a cancel/interrupt button.
        """
        return build_inline_keyboard(
            [[InlineButton("Cancel", encode_cancel_callback("interrupt"))]]
        )

    def _parse_command_args(self, args: str) -> list[str]:
        """Parse command arguments with shlex.

        Args:
            args: Command argument string.

        Returns:
            List of parsed argument tokens.
        """
        if not args:
            return []
        try:
            return [part for part in shlex.split(args) if part]
        except ValueError:
            return [part for part in args.split() if part]
