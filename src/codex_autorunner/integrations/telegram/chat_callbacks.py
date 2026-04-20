"""Telegram callback codec bridging wire payloads and logical callback ids."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..chat.callbacks import (
    CALLBACK_AGENT,
    CALLBACK_AGENT_PROFILE,
    CALLBACK_APPROVAL,
    CALLBACK_BIND,
    CALLBACK_CANCEL,
    CALLBACK_COMPACT,
    CALLBACK_DOCUMENT_BROWSER,
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
    "agent_profile": CALLBACK_AGENT_PROFILE,
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
    "document_browser": CALLBACK_DOCUMENT_BROWSER,
}


@dataclass(frozen=True)
class TelegramCallbackContractScenario:
    label: str
    callback_id: str
    payload: dict[str, Any]
    catalog_in_contract: bool = True


def cataloged_telegram_callback_contract_scenarios(
    *,
    include_control_variants: bool = True,
) -> tuple[TelegramCallbackContractScenario, ...]:
    scenarios: list[TelegramCallbackContractScenario] = [
        TelegramCallbackContractScenario(
            label="approval",
            callback_id=CALLBACK_APPROVAL,
            payload={"decision": "approve", "request_id": "req-1"},
        ),
        TelegramCallbackContractScenario(
            label="question_option",
            callback_id=CALLBACK_QUESTION_OPTION,
            payload={"request_id": "req-1", "question_index": 0, "option_index": 0},
        ),
        TelegramCallbackContractScenario(
            label="question_done",
            callback_id=CALLBACK_QUESTION_DONE,
            payload={"request_id": "req-1"},
        ),
        TelegramCallbackContractScenario(
            label="question_custom",
            callback_id=CALLBACK_QUESTION_CUSTOM,
            payload={"request_id": "req-1"},
        ),
        TelegramCallbackContractScenario(
            label="question_cancel",
            callback_id=CALLBACK_QUESTION_CANCEL,
            payload={"request_id": "req-1"},
        ),
        TelegramCallbackContractScenario(
            label="resume",
            callback_id=CALLBACK_RESUME,
            payload={"thread_id": "thread-1"},
        ),
        TelegramCallbackContractScenario(
            label="bind",
            callback_id=CALLBACK_BIND,
            payload={"repo_id": "repo-1"},
        ),
        TelegramCallbackContractScenario(
            label="agent",
            callback_id=CALLBACK_AGENT,
            payload={"agent": "codex"},
        ),
        TelegramCallbackContractScenario(
            label="agent_profile",
            callback_id=CALLBACK_AGENT_PROFILE,
            payload={"profile": "default"},
        ),
        TelegramCallbackContractScenario(
            label="model",
            callback_id=CALLBACK_MODEL,
            payload={"model_id": "gpt-5.4"},
        ),
        TelegramCallbackContractScenario(
            label="effort",
            callback_id=CALLBACK_EFFORT,
            payload={"effort": "high"},
        ),
        TelegramCallbackContractScenario(
            label="update",
            callback_id=CALLBACK_UPDATE,
            payload={"target": "discord"},
        ),
        TelegramCallbackContractScenario(
            label="update_confirm",
            callback_id=CALLBACK_UPDATE_CONFIRM,
            payload={"decision": "confirm"},
        ),
        TelegramCallbackContractScenario(
            label="review_commit",
            callback_id=CALLBACK_REVIEW_COMMIT,
            payload={"sha": "abc123"},
        ),
        TelegramCallbackContractScenario(
            label="compact",
            callback_id=CALLBACK_COMPACT,
            payload={"action": "start"},
        ),
        TelegramCallbackContractScenario(
            label="flow",
            callback_id=CALLBACK_FLOW,
            payload={"action": "resume", "run_id": "run-1", "repo_id": None},
        ),
        TelegramCallbackContractScenario(
            label="flow_run",
            callback_id=CALLBACK_FLOW_RUN,
            payload={"run_id": "run-1"},
        ),
        TelegramCallbackContractScenario(
            label="document_browser",
            callback_id=CALLBACK_DOCUMENT_BROWSER,
            payload={"action": "back", "value": None},
        ),
    ]
    if include_control_variants:
        scenarios.extend(
            (
                TelegramCallbackContractScenario(
                    label="flow_refresh",
                    callback_id=CALLBACK_FLOW,
                    payload={"action": "refresh", "run_id": "run-1", "repo_id": None},
                ),
                TelegramCallbackContractScenario(
                    label="selection_cancel",
                    callback_id=CALLBACK_CANCEL,
                    payload={"kind": "selection"},
                ),
                TelegramCallbackContractScenario(
                    label="interrupt",
                    callback_id=CALLBACK_CANCEL,
                    payload={"kind": "interrupt"},
                ),
                TelegramCallbackContractScenario(
                    label="queue_cancel",
                    callback_id=CALLBACK_CANCEL,
                    payload={"kind": "queue_cancel:exec-1"},
                ),
                TelegramCallbackContractScenario(
                    label="queue_interrupt_send",
                    callback_id=CALLBACK_CANCEL,
                    payload={"kind": "queue_interrupt_send:exec-1"},
                ),
                TelegramCallbackContractScenario(
                    label="pagination",
                    callback_id=CALLBACK_PAGE,
                    payload={"kind": "resume", "page": 1},
                ),
            )
        )
    return tuple(scenario for scenario in scenarios if scenario.catalog_in_contract)


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
        AgentProfileCallback,
        ApprovalCallback,
        BindCallback,
        CancelCallback,
        CompactCallback,
        DocumentBrowserCallback,
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
        CALLBACK_AGENT_PROFILE: AgentProfileCallback,
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
        CALLBACK_DOCUMENT_BROWSER: DocumentBrowserCallback,
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


def _encode_agent_profile(payload: dict[str, Any]) -> str:
    return f"agent_profile:{_required_str(payload, 'profile')}"


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


def _encode_document_browser(payload: dict[str, Any]) -> str:
    action = _required_str(payload, "action")
    value = _optional_str(payload, "value")
    if value is None:
        return f"doc:{action}"
    return f"doc:{action}:{value}"


_ENCODERS: dict[str, Callable[[dict[str, Any]], str]] = {
    CALLBACK_APPROVAL: _encode_approval,
    CALLBACK_QUESTION_OPTION: _encode_question_option,
    CALLBACK_QUESTION_DONE: _encode_question_done,
    CALLBACK_QUESTION_CUSTOM: _encode_question_custom,
    CALLBACK_QUESTION_CANCEL: _encode_question_cancel,
    CALLBACK_RESUME: _encode_resume,
    CALLBACK_BIND: _encode_bind,
    CALLBACK_AGENT: _encode_agent,
    CALLBACK_AGENT_PROFILE: _encode_agent_profile,
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
    CALLBACK_DOCUMENT_BROWSER: _encode_document_browser,
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
