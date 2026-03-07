from __future__ import annotations

from typing import Any

PMA_PUBLISH_RETRY_DELAYS_SECONDS = (0.0, 0.25, 0.75)


async def publish_automation_result(
    *,
    request: Any,
    result: dict[str, Any],
    client_turn_id: Any,
    lifecycle_event: Any,
    wake_up: Any,
) -> dict[str, Any]:
    return {}
