from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.update_paths import resolve_update_paths
from .types import CompactStatusState


def compact_status_path() -> Path:
    return resolve_update_paths().compact_status_path


def read_compact_status(path: Path) -> Optional[CompactStatusState]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return CompactStatusState.from_payload(data)


def write_compact_status(
    *,
    path: Path,
    logger: logging.Logger,
    status: str,
    message: str,
    **extra: Any,
) -> CompactStatusState:
    payload = CompactStatusState(
        status=status,
        message=message,
        at=time.time(),
        chat_id=_coerce_optional_int(extra.get("chat_id")),
        thread_id=_coerce_optional_int(extra.get("thread_id")),
        message_id=_coerce_optional_int(extra.get("message_id")),
        display_text=(
            extra.get("display_text")
            if isinstance(extra.get("display_text"), str)
            else None
        ),
        error_detail=(
            extra.get("error_detail")
            if isinstance(extra.get("error_detail"), str)
            else None
        ),
        started_at=_coerce_optional_float(extra.get("started_at")),
        notify_sent_at=_coerce_optional_float(extra.get("notify_sent_at")),
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload.to_payload()), encoding="utf-8")
    except (OSError, TypeError) as exc:
        log_event(
            logger,
            logging.WARNING,
            "telegram.compact.status_write_failed",
            exc=exc,
        )
    return payload


def mark_compact_notified(
    *, path: Path, logger: logging.Logger, status: CompactStatusState
) -> None:
    updated = status.with_notify_sent_at(time.time())
    try:
        path.write_text(json.dumps(updated.to_payload()), encoding="utf-8")
    except (OSError, TypeError) as exc:
        log_event(
            logger,
            logging.WARNING,
            "telegram.compact.notify_write_failed",
            exc=exc,
        )


def _coerce_optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _coerce_optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
