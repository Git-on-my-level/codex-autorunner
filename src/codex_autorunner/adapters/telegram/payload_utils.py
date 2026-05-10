"""Shared utility functions for extracting data from OpenCode payloads."""

from typing import Any, Optional


def extract_opencode_error_detail(payload: Any) -> Optional[str]:
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


def extract_opencode_session_path(payload: Any) -> Optional[str]:
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
        return extract_opencode_session_path(session)
    return None
