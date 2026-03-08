from __future__ import annotations

from fastapi import APIRouter


def build_chat_runtime_router(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build PMA chat runtime routes.

    This includes:
    - /active - Get current PMA status
    - /chat - Submit a PMA chat message
    - /interrupt - Interrupt running PMA turn
    - /stop - Stop a PMA lane
    - /new - Create new PMA session
    - /reset - Reset PMA state
    - /compact - Compact PMA history
    - /thread/reset - Reset PMA thread
    - /queue - Get queue summary
    - /queue/{lane_id} - Get lane queue items
    - /turns/{turn_id}/events - Stream turn events

    Currently these routes remain inline in pma.py to maintain tight coupling
    with lane worker management and queue execution helpers.
    """
    pass
