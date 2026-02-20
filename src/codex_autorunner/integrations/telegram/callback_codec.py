"""Shared callback payload parsing helpers for Telegram adapters."""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

CallbackFields = dict[str, Any]
_CallbackParser = Callable[[str], Optional[Tuple[str, CallbackFields]]]


def parse_callback_payload(data: Optional[str]) -> Optional[Tuple[str, dict[str, Any]]]:
    """Parse callback payload into ``(kind, fields)`` for construction."""
    if not data:
        return None
    prefix, sep, rest = data.partition(":")
    if not sep:
        return None

    parser = _PAYLOAD_PARSERS.get(prefix)
    if parser is None:
        return None

    parsed = parser(rest)
    if parsed is None:
        return None
    return parsed


def _parse_approval(rest: str) -> Optional[Tuple[str, CallbackFields]]:
    decision, sep, request_id = rest.partition(":")
    if not decision or not sep or not request_id:
        return None
    return "approval", {"decision": decision, "request_id": request_id}


def _parse_question_option(rest: str) -> Optional[Tuple[str, CallbackFields]]:
    question_raw, sep, rest = rest.partition(":")
    option_raw, sep2, request_id = rest.partition(":")
    if not question_raw or not sep or not option_raw or not sep2 or not request_id:
        return None
    if not question_raw.isdigit() or not option_raw.isdigit():
        return None
    return (
        "question_option",
        {
            "request_id": request_id,
            "question_index": int(question_raw),
            "option_index": int(option_raw),
        },
    )


def _parse_tokened_data(
    rest: str, kind: str, field_name: str
) -> Optional[Tuple[str, CallbackFields]]:
    if not rest:
        return None
    return kind, {field_name: rest}


def _parse_page(rest: str) -> Optional[Tuple[str, CallbackFields]]:
    kind, sep, page_raw = rest.partition(":")
    if not kind or not sep or not page_raw:
        return None
    if not page_raw.isdigit():
        return None
    return "page", {"kind": kind, "page": int(page_raw)}


def _parse_flow(rest: str) -> Optional[Tuple[str, CallbackFields]]:
    action, sep, tail = rest.partition(":")
    if not action:
        return None
    if not sep:
        return "flow", {"action": action, "run_id": None, "repo_id": None}
    run_id, sep2, repo_id = tail.partition(":")
    if not run_id:
        return None
    if sep2 and not repo_id:
        return None
    return "flow", {
        "action": action,
        "run_id": run_id,
        "repo_id": repo_id or None,
    }


_PAYLOAD_PARSERS: dict[str, _CallbackParser] = {
    "appr": _parse_approval,
    "qopt": _parse_question_option,
    "qdone": lambda rest: _parse_tokened_data(rest, "question_done", "request_id"),
    "qcustom": lambda rest: _parse_tokened_data(rest, "question_custom", "request_id"),
    "qcancel": lambda rest: _parse_tokened_data(rest, "question_cancel", "request_id"),
    "resume": lambda rest: _parse_tokened_data(rest, "resume", "thread_id"),
    "bind": lambda rest: _parse_tokened_data(rest, "bind", "repo_id"),
    "agent": lambda rest: _parse_tokened_data(rest, "agent", "agent"),
    "model": lambda rest: _parse_tokened_data(rest, "model", "model_id"),
    "effort": lambda rest: _parse_tokened_data(rest, "effort", "effort"),
    "update": lambda rest: _parse_tokened_data(rest, "update", "target"),
    "update_confirm": lambda rest: _parse_tokened_data(
        rest, "update_confirm", "decision"
    ),
    "review_commit": lambda rest: _parse_tokened_data(rest, "review_commit", "sha"),
    "cancel": lambda rest: _parse_tokened_data(rest, "cancel", "kind"),
    "compact": lambda rest: _parse_tokened_data(rest, "compact", "action"),
    "page": _parse_page,
    "flow": _parse_flow,
    "flow_run": lambda rest: _parse_tokened_data(rest, "flow_run", "run_id"),
}
