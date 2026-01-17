from __future__ import annotations

import contextvars
from typing import Optional

_REQUEST_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "codex_autorunner_request_id",
    default=None,
)
_CONVERSATION_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "codex_autorunner_conversation_id",
    default=None,
)


def set_request_id(request_id: Optional[str]) -> contextvars.Token[Optional[str]]:
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: contextvars.Token[Optional[str]]) -> None:
    _REQUEST_ID.reset(token)


def get_request_id() -> Optional[str]:
    return _REQUEST_ID.get()


def set_conversation_id(
    conversation_id: Optional[str],
) -> contextvars.Token[Optional[str]]:
    return _CONVERSATION_ID.set(conversation_id)


def reset_conversation_id(token: contextvars.Token[Optional[str]]) -> None:
    _CONVERSATION_ID.reset(token)


def get_conversation_id() -> Optional[str]:
    return _CONVERSATION_ID.get()
