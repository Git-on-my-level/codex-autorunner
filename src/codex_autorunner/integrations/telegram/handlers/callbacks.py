from __future__ import annotations

from typing import Any, Sequence

from ..adapter import (
    AgentCallback,
    ApprovalCallback,
    BindCallback,
    CancelCallback,
    CompactCallback,
    EffortCallback,
    ModelCallback,
    PageCallback,
    PrFlowStartCallback,
    QuestionCancelCallback,
    QuestionCustomCallback,
    QuestionDoneCallback,
    QuestionOptionCallback,
    ResumeCallback,
    ReviewCommitCallback,
    TelegramCallbackQuery,
    UpdateCallback,
    UpdateConfirmCallback,
    parse_callback_data,
)


def _selection_contains(items: Sequence[tuple[str, str]], value: str) -> bool:
    return any(item_id == value for item_id, _ in items)


async def handle_callback(handlers: Any, callback: TelegramCallbackQuery) -> None:
    parsed = parse_callback_data(callback.data)
    if parsed is None:
        return
    key = None
    if callback.chat_id is not None:
        key = await handlers._resolve_topic_key(callback.chat_id, callback.thread_id)
    if isinstance(parsed, ApprovalCallback):
        await handlers._handle_approval_callback(callback, parsed)
    elif isinstance(
        parsed,
        (
            QuestionOptionCallback,
            QuestionCancelCallback,
            QuestionCustomCallback,
            QuestionDoneCallback,
        ),
    ):
        await handlers._handle_question_callback(callback, parsed)
    elif isinstance(parsed, ResumeCallback):
        if key:
            state = handlers._resume_options.get(key)
            if not state or not _selection_contains(state.items, parsed.thread_id):
                await handlers._answer_callback(callback, "Selection expired")
                return
            await handlers._resume_thread_by_id(key, parsed.thread_id, callback)
    elif isinstance(parsed, BindCallback):
        if key:
            state = handlers._bind_options.get(key)
            if not state or not _selection_contains(state.items, parsed.repo_id):
                await handlers._answer_callback(callback, "Selection expired")
                return
            await handlers._bind_topic_by_repo_id(key, parsed.repo_id, callback)
    elif isinstance(parsed, AgentCallback):
        if key:
            await handlers._handle_agent_callback(key, callback, parsed)
    elif isinstance(parsed, ModelCallback):
        if key:
            await handlers._handle_model_callback(key, callback, parsed)
    elif isinstance(parsed, EffortCallback):
        if key:
            await handlers._handle_effort_callback(key, callback, parsed)
    elif isinstance(parsed, UpdateCallback):
        if key:
            await handlers._handle_update_callback(key, callback, parsed)
    elif isinstance(parsed, UpdateConfirmCallback):
        if key:
            await handlers._handle_update_confirm_callback(key, callback, parsed)
    elif isinstance(parsed, ReviewCommitCallback):
        if key:
            await handlers._handle_review_commit_callback(key, callback, parsed)
    elif isinstance(parsed, PrFlowStartCallback):
        # Ticket-first: PR flow is deprecated.
        await handlers._answer_callback(
            callback,
            "PR flow is deprecated; use ticket flow (/flow) instead.",
        )
        return
    elif isinstance(parsed, CancelCallback):
        if key:
            if parsed.kind == "interrupt":
                await handlers._handle_interrupt_callback(callback)
            else:
                await handlers._handle_selection_cancel(key, parsed, callback)
    elif isinstance(parsed, CompactCallback):
        if key:
            await handlers._handle_compact_callback(key, callback, parsed)
    elif isinstance(parsed, PageCallback):
        if key:
            await handlers._handle_selection_page(key, parsed, callback)
