from __future__ import annotations

from typing import Optional


def ok_response(**data: object) -> dict[str, object]:
    return {"status": "ok", **data}


def error_response(detail: str, **data: object) -> dict[str, object]:
    return {"status": "error", "detail": detail, **data}


def error_detail(
    code: str, message: str, meta: Optional[dict[str, object]] = None
) -> dict[str, object]:
    payload: dict[str, object] = {"code": code, "message": message}
    if meta:
        payload["meta"] = meta
    return payload
