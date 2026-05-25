from __future__ import annotations

from typing import Any


def is_retrying_turn_error(method: str, params: dict[str, Any]) -> bool:
    """Return whether a runtime error event is explicitly non-terminal."""

    return method == "turn/error" and params.get("willRetry") is True
