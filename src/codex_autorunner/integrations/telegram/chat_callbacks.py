"""Telegram callback codec bridging wire payloads and logical callback ids."""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..chat.callbacks import (
    CALLBACK_AGENT,
    CALLBACK_APPROVAL,
    CALLBACK_BIND,
    CALLBACK_CANCEL,
    CALLBACK_COMPACT,
    CALLBACK_EFFORT,
    CALLBACK_FLOW,
    CALLBACK_FLOW_RUN,
    CALLBACK_MODEL,
    CALLBACK_PAGE,
    CALLBACK_QUESTION_CANCEL,
    CALLBACK_QUESTION_CUSTOM,
    CALLBACK_QUESTION_DONE,
    CALLBACK_QUESTION_OPTION,
    CALLBACK_RESUME,
    CALLBACK_REVIEW_COMMIT,
    CALLBACK_UPDATE,
    CALLBACK_UPDATE_CONFIRM,
    CallbackCodec,
    LogicalCallback,
)
from .callback_codec import parse_callback_payload
from .constants import TELEGRAM_CALLBACK_DATA_LIMIT

_KIND_TO_ID = {
    "approval": CALLBACK_APPROVAL,
    "question_option": CALLBACK_QUESTION_OPTION,
    "question_done": CALLBACK_QUESTION_DONE,
    "question_custom": CALLBACK_QUESTION_CUSTOM,
    "question_cancel": CALLBACK_QUESTION_CANCEL,
    "resume": CALLBACK_RESUME,
    "bind": CALLBACK_BIND,
    "agent": CALLBACK_AGENT,
    "model": CALLBACK_MODEL,
    "effort": CALLBACK_EFFORT,
    "update": CALLBACK_UPDATE,
    "update_confirm": CALLBACK_UPDATE_CONFIRM,
    "review_commit": CALLBACK_REVIEW_COMMIT,
    "cancel": CALLBACK_CANCEL,
    "compact": CALLBACK_COMPACT,
    "page": CALLBACK_PAGE,
    "flow": CALLBACK_FLOW,
    "flow_run": CALLBACK_FLOW_RUN,
}


class TelegramCallbackCodec(CallbackCodec):
    """Codec preserving Telegram callback wire compatibility."""

    def decode(self, platform_payload: Optional[str]) -> Optional[LogicalCallback]:
        parsed = parse_callback_payload(platform_payload)
        if parsed is None:
            return None
        kind, fields = parsed
        callback_id = _KIND_TO_ID.get(kind)
        if callback_id is None:
            return None
        return LogicalCallback(callback_id=callback_id, payload=fields)

    def encode(self, callback: LogicalCallback) -> str:
        handler = _ENCODERS.get(callback.callback_id)
        if handler is None:
            raise ValueError(f"unsupported callback id: {callback.callback_id}")
        data = handler(callback.payload)
        _validate_callback_data(data)
        return data


def parse_callback_data(data: Optional[str]) -> Optional[Any]:
    """Parse Telegram callback wire payload into legacy callback dataclasses."""

    decoded = TelegramCallbackCodec().decode(data)
    if decoded is None:
        return None
    constructors = _legacy_constructors()
    constructor = constructors.get(decoded.callback_id)
    if constructor is None:
        return None
    return constructor(**decoded.payload)


def _legacy_constructors() -> dict[str, Callable[..., Any]]:
    from .adapter import (
        AgentCallback,
        ApprovalCallback,
        BindCallback,
        CancelCallback,
        CompactCallback,
        EffortCallback,
        FlowCallback,
        FlowRunCallback,
        ModelCallback,
        PageCallback,
        QuestionCancelCallback,
        QuestionCustomCallback,
        QuestionDoneCallback,
        QuestionOptionCallback,
        ResumeCallback,
        ReviewCommitCallback,
        UpdateCallback,
        UpdateConfirmCallback,
    )

    return {
        CALLBACK_APPROVAL: ApprovalCallback,
        CALLBACK_QUESTION_OPTION: QuestionOptionCallback,
        CALLBACK_QUESTION_DONE: QuestionDoneCallback,
        CALLBACK_QUESTION_CUSTOM: QuestionCustomCallback,
        CALLBACK_QUESTION_CANCEL: QuestionCancelCallback,
        CALLBACK_RESUME: ResumeCallback,
        CALLBACK_BIND: BindCallback,
        CALLBACK_AGENT: AgentCallback,
        CALLBACK_MODEL: ModelCallback,
        CALLBACK_EFFORT: EffortCallback,
        CALLBACK_UPDATE: UpdateCallback,
        CALLBACK_UPDATE_CONFIRM: UpdateConfirmCallback,
        CALLBACK_REVIEW_COMMIT: ReviewCommitCallback,
        CALLBACK_CANCEL: CancelCallback,
        CALLBACK_COMPACT: CompactCallback,
        CALLBACK_PAGE: PageCallback,
        CALLBACK_FLOW: FlowCallback,
        CALLBACK_FLOW_RUN: FlowRunCallback,
    }


def _encode_approval(payload: dict[str, Any]) -> str:
    decision = _required_str(payload, "decision")
    request_id = _required_str(payload, "request_id")
    return f"appr:{decision}:{request_id}"


def _encode_question_option(payload: dict[str, Any]) -> str:
    request_id = _required_str(payload, "request_id")
    question_index = _required_int(payload, "question_index")
    option_index = _required_int(payload, "option_index")
    return f"qopt:{question_index}:{option_index}:{request_id}"


def _encode_question_done(payload: dict[str, Any]) -> str:
    return f"qdone:{_required_str(payload, 'request_id')}"


def _encode_question_custom(payload: dict[str, Any]) -> str:
    return f"qcustom:{_required_str(payload, 'request_id')}"


def _encode_question_cancel(payload: dict[str, Any]) -> str:
    return f"qcancel:{_required_str(payload, 'request_id')}"


def _encode_resume(payload: dict[str, Any]) -> str:
    return f"resume:{_required_str(payload, 'thread_id')}"


def _encode_bind(payload: dict[str, Any]) -> str:
    return f"bind:{_required_str(payload, 'repo_id')}"


def _encode_agent(payload: dict[str, Any]) -> str:
    return f"agent:{_required_str(payload, 'agent')}"


def _encode_model(payload: dict[str, Any]) -> str:
    return f"model:{_required_str(payload, 'model_id')}"


def _encode_effort(payload: dict[str, Any]) -> str:
    return f"effort:{_required_str(payload, 'effort')}"


def _encode_update(payload: dict[str, Any]) -> str:
    return f"update:{_required_str(payload, 'target')}"


def _encode_update_confirm(payload: dict[str, Any]) -> str:
    return f"update_confirm:{_required_str(payload, 'decision')}"


def _encode_review_commit(payload: dict[str, Any]) -> str:
    return f"review_commit:{_required_str(payload, 'sha')}"


def _encode_cancel(payload: dict[str, Any]) -> str:
    return f"cancel:{_required_str(payload, 'kind')}"


def _encode_compact(payload: dict[str, Any]) -> str:
    return f"compact:{_required_str(payload, 'action')}"


def _encode_page(payload: dict[str, Any]) -> str:
    kind = _required_str(payload, "kind")
    page = _required_int(payload, "page")
    return f"page:{kind}:{page}"


def _encode_flow(payload: dict[str, Any]) -> str:
    action = _required_str(payload, "action")
    run_id = _optional_str(payload, "run_id")
    repo_id = _optional_str(payload, "repo_id")
    if repo_id and not run_id:
        raise ValueError("flow repo callback requires run_id")
    if run_id:
        data = f"flow:{action}:{run_id}"
        if repo_id:
            data = f"{data}:{repo_id}"
        return data
    return f"flow:{action}"


def _encode_flow_run(payload: dict[str, Any]) -> str:
    run_id = _required_str(payload, "run_id")
    return f"flow_run:{run_id}"


_ENCODERS: dict[str, Callable[[dict[str, Any]], str]] = {
    CALLBACK_APPROVAL: _encode_approval,
    CALLBACK_QUESTION_OPTION: _encode_question_option,
    CALLBACK_QUESTION_DONE: _encode_question_done,
    CALLBACK_QUESTION_CUSTOM: _encode_question_custom,
    CALLBACK_QUESTION_CANCEL: _encode_question_cancel,
    CALLBACK_RESUME: _encode_resume,
    CALLBACK_BIND: _encode_bind,
    CALLBACK_AGENT: _encode_agent,
    CALLBACK_MODEL: _encode_model,
    CALLBACK_EFFORT: _encode_effort,
    CALLBACK_UPDATE: _encode_update,
    CALLBACK_UPDATE_CONFIRM: _encode_update_confirm,
    CALLBACK_REVIEW_COMMIT: _encode_review_commit,
    CALLBACK_CANCEL: _encode_cancel,
    CALLBACK_COMPACT: _encode_compact,
    CALLBACK_PAGE: _encode_page,
    CALLBACK_FLOW: _encode_flow,
    CALLBACK_FLOW_RUN: _encode_flow_run,
}


def _validate_callback_data(data: str) -> None:
    if len(data.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_LIMIT:
        raise ValueError("callback_data exceeds Telegram limit")


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing callback field: {key}")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"missing callback field: {key}")
    return value


def _optional_str(payload: dict[str, Any], key: str) -> Optional[str]:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid callback field: {key}")
    normalized = value.strip()
    return normalized or None
