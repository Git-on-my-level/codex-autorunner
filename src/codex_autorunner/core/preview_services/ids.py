from __future__ import annotations

import re
import secrets

SERVICE_ID_PREFIX = "svc_"
SERVICE_ID_PATTERN = re.compile(r"^svc_[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")

WORKSPACE_ID_PREFIX = "ws_"
WORKSPACE_ID_PATTERN = re.compile(r"^ws_[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")


def generate_service_id() -> str:
    return f"{SERVICE_ID_PREFIX}{secrets.token_hex(8)}"


def validate_service_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("service_id must be a non-empty string")
    text = value.strip()
    if not SERVICE_ID_PATTERN.fullmatch(text):
        raise ValueError(
            "service_id must start with 'svc_' and contain only letters, "
            "numbers, underscores, and hyphens"
        )
    return text


def generate_workspace_id() -> str:
    return f"{WORKSPACE_ID_PREFIX}{secrets.token_hex(8)}"


def validate_workspace_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("workspace_id must be a non-empty string")
    text = value.strip()
    if not WORKSPACE_ID_PATTERN.fullmatch(text):
        raise ValueError(
            "workspace_id must start with 'ws_' and contain only letters, "
            "numbers, underscores, and hyphens"
        )
    return text
