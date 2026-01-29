from typing import Any, Optional


def extract_turn_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("turnId", "turn_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    turn = payload.get("turn")
    if isinstance(turn, dict):
        for key in ("id", "turnId", "turn_id"):
            value = turn.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_thread_id_from_container(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None


def extract_thread_id_for_turn(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("turn"), payload.get("item")):
        thread_id = _extract_thread_id_from_container(candidate)
        if thread_id:
            return thread_id
    return None


def extract_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("threadId", "thread_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    thread = payload.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "threadId", "thread_id"):
            value = thread.get(key)
            if isinstance(value, str):
                return value
    return None
