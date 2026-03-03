from __future__ import annotations

from typing import Callable, Optional

from fastapi import HTTPException

DetailBuilder = Callable[[str], object]


def _validation_http_error(
    message: str, *, detail_builder: Optional[DetailBuilder] = None
) -> HTTPException:
    detail: object = detail_builder(message) if detail_builder else message
    return HTTPException(status_code=400, detail=detail)


def normalize_optional_string(
    value: object,
    field: str,
    *,
    allow_blank: bool = True,
    require_single_line: bool = False,
    detail_builder: Optional[DetailBuilder] = None,
) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation_http_error(
            f"{field} must be a string", detail_builder=detail_builder
        )
    cleaned = value.strip()
    if not cleaned:
        if allow_blank:
            return None
        raise _validation_http_error(
            f"{field} must not be empty", detail_builder=detail_builder
        )
    if require_single_line and ("\n" in cleaned or "\r" in cleaned):
        raise _validation_http_error(
            f"{field} must be single-line", detail_builder=detail_builder
        )
    return cleaned


def normalize_required_string(
    value: object,
    field: str,
    *,
    require_single_line: bool = False,
    detail_builder: Optional[DetailBuilder] = None,
) -> str:
    if not isinstance(value, str):
        raise _validation_http_error(
            f"{field} must be a string", detail_builder=detail_builder
        )
    cleaned = value.strip()
    if not cleaned:
        raise _validation_http_error(
            f"{field} must not be empty", detail_builder=detail_builder
        )
    if require_single_line and ("\n" in cleaned or "\r" in cleaned):
        raise _validation_http_error(
            f"{field} must be single-line", detail_builder=detail_builder
        )
    return cleaned


def normalize_string_lower(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def normalize_agent_id(value: object, default: str = "codex") -> str:
    return normalize_string_lower(value) or default
