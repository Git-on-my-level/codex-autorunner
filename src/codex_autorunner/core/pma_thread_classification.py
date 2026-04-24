from __future__ import annotations

from typing import Any, Mapping, Optional

FOLLOWUP_STATE_RUNNING_HEALTHY = "running_healthy"
FOLLOWUP_STATE_ATTENTION_REQUIRED = "attention_required"
FOLLOWUP_STATE_AWAITING_FOLLOWUP = "awaiting_followup"
FOLLOWUP_STATE_REUSABLE = "reusable"
FOLLOWUP_STATE_PROTECTED_CHAT_BOUND = "protected_chat_bound"
FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE = "idle_archive_candidate"

_PROTECTION_REASON_BOUND_OR_BUSY = (
    "Managed thread still has an active binding or work in flight."
)
_PROTECTION_REASON_CHAT_BOUND = (
    "Chat-bound managed thread is dormant, but continuity-bearing "
    "chat threads are protected from cleanup by default"
)
_PROTECTION_REASON_UNPROTECTED = (
    "Idle managed-thread followup is dormant but may still be reusable."
)


def is_pma_self_thread(entry: Mapping[str, Any]) -> bool:
    agent = str(entry.get("agent") or entry.get("agent_id") or "").strip().lower()
    thread_kind = str(entry.get("thread_kind") or "").strip().lower()
    resource_kind = str(entry.get("resource_kind") or "").strip().lower()
    if thread_kind == "pma":
        return True
    return resource_kind == "agent_workspace" and agent.endswith("-pma")


def is_thread_cleanup_protected(
    *,
    has_binding: bool = False,
    has_busy_work: bool = False,
    is_chat_bound: bool = False,
) -> bool:
    return has_binding or has_busy_work or is_chat_bound


def thread_cleanup_protection_reason(
    *,
    has_binding: bool = False,
    has_busy_work: bool = False,
    is_chat_bound: bool = False,
) -> str:
    if has_binding or has_busy_work:
        return _PROTECTION_REASON_BOUND_OR_BUSY
    if is_chat_bound:
        return _PROTECTION_REASON_CHAT_BOUND
    return _PROTECTION_REASON_UNPROTECTED


def thread_followup_state_rank(state: str) -> int:
    normalized = state.strip().lower()
    ranks = {
        FOLLOWUP_STATE_ATTENTION_REQUIRED: 0,
        FOLLOWUP_STATE_AWAITING_FOLLOWUP: 10,
        FOLLOWUP_STATE_REUSABLE: 20,
        FOLLOWUP_STATE_PROTECTED_CHAT_BOUND: 25,
        FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE: 30,
        FOLLOWUP_STATE_RUNNING_HEALTHY: 40,
    }
    return ranks.get(normalized, 99)


def classify_thread_followup(
    entry: Mapping[str, Any],
    *,
    is_stale: bool,
    is_chat_bound: bool,
    is_self_thread: bool = False,
) -> dict[str, Any]:
    status = (
        str(entry.get("normalized_status") or entry.get("status") or "").strip().lower()
    )
    status_reason = (
        str(entry.get("status_reason_code") or entry.get("status_reason") or "")
        .strip()
        .lower()
    )
    last_turn_id = str(entry.get("last_turn_id") or "").strip()

    if status == "running":
        return _classify_running(is_stale=is_stale, is_self_thread=is_self_thread)

    if status == "failed":
        return _followup(
            FOLLOWUP_STATE_ATTENTION_REQUIRED,
            "urgent",
            "inspect_managed_thread_failure",
            "Managed thread failed and needs inspection before reuse",
            detail_template=(
                "car pma thread status --id {managed_thread_id} --path <hub_root>"
            ),
        )

    if status == "paused":
        return _followup(
            FOLLOWUP_STATE_AWAITING_FOLLOWUP,
            "normal",
            "resume_managed_thread",
            "Managed thread is paused and likely waiting for the next turn",
            detail_template=(
                'car pma thread send --id {managed_thread_id} --message "..." '
                "--path <hub_root>"
            ),
        )

    if status in {"completed", "interrupted"}:
        return _classify_dormant_or_reusable(
            is_stale=is_stale, is_chat_bound=is_chat_bound
        )

    if status == "idle":
        if (
            (status_reason == "thread_created" and not last_turn_id)
            or status_reason == "thread_resumed"
        ) and not is_stale:
            return _reusable(
                (
                    "Managed thread was recently created or resumed, but there is "
                    "no explicit wake-up or continuity signal that PMA needs to "
                    "act on it now"
                )
            )
        return _classify_dormant_or_reusable(
            is_stale=is_stale, is_chat_bound=is_chat_bound
        )

    return _reusable("Managed thread can accept another turn if PMA needs it")


def _classify_running(*, is_stale: bool, is_self_thread: bool) -> dict[str, Any]:
    if is_stale:
        if is_self_thread:
            return _followup(
                FOLLOWUP_STATE_RUNNING_HEALTHY,
                "none",
                None,
                "PMA self thread is still running; suppress hung-thread noise",
            )
        return _followup(
            FOLLOWUP_STATE_ATTENTION_REQUIRED,
            "urgent",
            "inspect_likely_hung_thread",
            (
                "Managed thread has been running for an unusually long time "
                "and is likely hung; inspect or interrupt it"
            ),
            detail_template=(
                "car pma thread status --id {managed_thread_id} --path <hub_root> ; "
                "car pma thread interrupt --id {managed_thread_id} --path <hub_root>"
            ),
        )
    return _followup(
        FOLLOWUP_STATE_RUNNING_HEALTHY,
        "none",
        None,
        "Managed thread is actively running",
    )


def _classify_dormant_or_reusable(
    *, is_stale: bool, is_chat_bound: bool
) -> dict[str, Any]:
    if is_stale:
        if is_chat_bound:
            return _followup(
                FOLLOWUP_STATE_PROTECTED_CHAT_BOUND,
                "protected",
                "show_protected_threads",
                _PROTECTION_REASON_CHAT_BOUND,
                detail_template=(
                    "Protected by default: review this chat-bound thread before "
                    "taking any action. Do not archive or remove it unless the "
                    'user is explicit; broad requests like "clean up workspace" '
                    "are not enough."
                ),
            )
        return _followup(
            FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE,
            "cleanup",
            "review_or_archive_managed_thread",
            (
                "Managed thread has been dormant since its last completed turn "
                "and is a cleanup candidate"
            ),
            detail_template=(
                "Review before cleanup: car pma thread archive --id "
                "{managed_thread_id} --path <hub_root> if dormant, or reuse it with "
                'car pma thread send --id {managed_thread_id} --message "..." '
                "--path <hub_root>"
            ),
        )
    return _reusable(
        (
            "Managed thread can be reused, but there is no stronger signal "
            "that PMA needs to act on it now"
        )
    )


def _reusable(why_selected: str) -> dict[str, Any]:
    return _followup(
        FOLLOWUP_STATE_REUSABLE,
        "optional",
        "consider_resuming_managed_thread",
        why_selected,
        detail_template=(
            "Optional reuse: car pma thread send --id {managed_thread_id} "
            '--message "..." --path <hub_root>'
        ),
    )


def _followup(
    followup_state: str,
    operator_need: str,
    recommended_action: Optional[str],
    why_selected: str,
    *,
    detail_template: Optional[str] = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "followup_state": followup_state,
        "operator_need": operator_need,
        "recommended_action": recommended_action,
        "why_selected": why_selected,
    }
    if detail_template is not None:
        result["recommended_detail_template"] = detail_template
    return result
