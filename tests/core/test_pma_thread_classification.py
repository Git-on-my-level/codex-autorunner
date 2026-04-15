from __future__ import annotations

import pytest

from codex_autorunner.core.pma_thread_classification import (
    FOLLOWUP_STATE_ATTENTION_REQUIRED,
    FOLLOWUP_STATE_AWAITING_FOLLOWUP,
    FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE,
    FOLLOWUP_STATE_PROTECTED_CHAT_BOUND,
    FOLLOWUP_STATE_REUSABLE,
    FOLLOWUP_STATE_RUNNING_HEALTHY,
    classify_thread_followup,
    is_pma_self_thread,
    is_thread_cleanup_protected,
    thread_cleanup_protection_reason,
    thread_followup_state_rank,
)


class TestIsPmaSelfThread:
    def test_pma_agent_with_agent_workspace(self) -> None:
        assert is_pma_self_thread(
            {
                "agent": "hermes-m4-pma",
                "resource_kind": "agent_workspace",
            }
        )

    def test_pma_thread_kind(self) -> None:
        assert is_pma_self_thread({"thread_kind": "pma"})

    def test_non_pma_agent(self) -> None:
        assert not is_pma_self_thread(
            {
                "agent": "codex",
                "resource_kind": "repo",
            }
        )

    def test_non_pma_agent_workspace(self) -> None:
        assert not is_pma_self_thread(
            {
                "agent": "codex",
                "resource_kind": "agent_workspace",
            }
        )

    def test_empty_entry(self) -> None:
        assert not is_pma_self_thread({})

    def test_agent_id_fallback(self) -> None:
        assert is_pma_self_thread(
            {
                "agent_id": "test-pma",
                "resource_kind": "agent_workspace",
            }
        )


class TestIsThreadCleanupProtected:
    def test_protected_by_binding(self) -> None:
        assert is_thread_cleanup_protected(has_binding=True)

    def test_protected_by_busy_work(self) -> None:
        assert is_thread_cleanup_protected(has_busy_work=True)

    def test_protected_by_chat_bound(self) -> None:
        assert is_thread_cleanup_protected(is_chat_bound=True)

    def test_not_protected(self) -> None:
        assert not is_thread_cleanup_protected()

    def test_binding_overrides(self) -> None:
        assert is_thread_cleanup_protected(
            has_binding=True, has_busy_work=False, is_chat_bound=False
        )


class TestThreadCleanupProtectionReason:
    def test_binding_reason(self) -> None:
        reason = thread_cleanup_protection_reason(has_binding=True)
        assert "active binding" in reason

    def test_busy_work_reason(self) -> None:
        reason = thread_cleanup_protection_reason(has_busy_work=True)
        assert "active binding" in reason

    def test_chat_bound_reason(self) -> None:
        reason = thread_cleanup_protection_reason(is_chat_bound=True)
        assert "Chat-bound" in reason

    def test_unprotected_reason(self) -> None:
        reason = thread_cleanup_protection_reason()
        assert "dormant" in reason


class TestThreadFollowupStateRank:
    def test_attention_required_highest(self) -> None:
        assert thread_followup_state_rank("attention_required") == 0

    def test_awaiting_followup(self) -> None:
        assert thread_followup_state_rank("awaiting_followup") == 10

    def test_reusable(self) -> None:
        assert thread_followup_state_rank("reusable") == 20

    def test_protected_chat_bound(self) -> None:
        assert thread_followup_state_rank("protected_chat_bound") == 25

    def test_idle_archive_candidate(self) -> None:
        assert thread_followup_state_rank("idle_archive_candidate") == 30

    def test_unknown_state(self) -> None:
        assert thread_followup_state_rank("unknown_state") == 99


class TestClassifyThreadFollowup:
    def test_running_not_stale(self) -> None:
        result = classify_thread_followup(
            {"status": "running"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_RUNNING_HEALTHY
        assert result["operator_need"] == "none"

    def test_running_stale_self_thread(self) -> None:
        result = classify_thread_followup(
            {"status": "running"},
            is_stale=True,
            is_chat_bound=False,
            is_self_thread=True,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_RUNNING_HEALTHY
        assert result["operator_need"] == "none"

    def test_running_stale_non_self(self) -> None:
        result = classify_thread_followup(
            {"status": "running"},
            is_stale=True,
            is_chat_bound=False,
            is_self_thread=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_ATTENTION_REQUIRED
        assert result["operator_need"] == "urgent"
        assert result["recommended_action"] == "inspect_likely_hung_thread"

    def test_failed(self) -> None:
        result = classify_thread_followup(
            {"status": "failed"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_ATTENTION_REQUIRED
        assert result["operator_need"] == "urgent"

    def test_paused(self) -> None:
        result = classify_thread_followup(
            {"status": "paused"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_AWAITING_FOLLOWUP
        assert result["operator_need"] == "normal"
        assert result["recommended_action"] == "resume_managed_thread"

    def test_completed_stale_chat_bound(self) -> None:
        result = classify_thread_followup(
            {"status": "completed"},
            is_stale=True,
            is_chat_bound=True,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_PROTECTED_CHAT_BOUND
        assert result["operator_need"] == "protected"

    def test_completed_stale_unbound(self) -> None:
        result = classify_thread_followup(
            {"status": "completed"},
            is_stale=True,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE
        assert result["operator_need"] == "cleanup"

    def test_completed_fresh(self) -> None:
        result = classify_thread_followup(
            {"status": "completed"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_REUSABLE
        assert result["operator_need"] == "optional"

    def test_idle_stale_chat_bound(self) -> None:
        result = classify_thread_followup(
            {"status": "idle"},
            is_stale=True,
            is_chat_bound=True,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_PROTECTED_CHAT_BOUND

    def test_idle_stale_unbound(self) -> None:
        result = classify_thread_followup(
            {"status": "idle"},
            is_stale=True,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE

    def test_idle_recently_created(self) -> None:
        result = classify_thread_followup(
            {"status": "idle", "status_reason": "thread_created"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_REUSABLE

    def test_idle_recently_resumed(self) -> None:
        result = classify_thread_followup(
            {"status": "idle", "status_reason": "thread_resumed"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_REUSABLE

    def test_idle_fresh_other(self) -> None:
        result = classify_thread_followup(
            {"status": "idle", "status_reason": "other"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_REUSABLE

    def test_unknown_status(self) -> None:
        result = classify_thread_followup(
            {"status": "unknown"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_REUSABLE

    def test_interrupted_stale_chat_bound(self) -> None:
        result = classify_thread_followup(
            {"status": "interrupted"},
            is_stale=True,
            is_chat_bound=True,
        )
        assert result["followup_state"] == FOLLOWUP_STATE_PROTECTED_CHAT_BOUND

    def test_detail_template_present_for_attention_states(self) -> None:
        result = classify_thread_followup(
            {"status": "failed"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert "recommended_detail_template" in result
        assert "{managed_thread_id}" in result["recommended_detail_template"]

    def test_no_detail_template_for_running_healthy(self) -> None:
        result = classify_thread_followup(
            {"status": "running"},
            is_stale=False,
            is_chat_bound=False,
        )
        assert "recommended_detail_template" not in result
        assert result["recommended_action"] is None


class TestProjectionAlignment:
    def test_hygiene_and_action_queue_agree_on_thread_protection(self) -> None:
        has_binding = True
        has_busy_work = False
        is_chat_bound = has_binding

        assert is_thread_cleanup_protected(
            has_binding=has_binding,
            has_busy_work=has_busy_work,
            is_chat_bound=is_chat_bound,
        )

        followup = classify_thread_followup(
            {"status": "completed"},
            is_stale=True,
            is_chat_bound=is_chat_bound,
        )
        assert followup["followup_state"] == FOLLOWUP_STATE_PROTECTED_CHAT_BOUND

    def test_hygiene_and_action_queue_agree_on_unprotected_cleanup(self) -> None:
        has_binding = False
        has_busy_work = False
        is_chat_bound = False

        assert not is_thread_cleanup_protected(
            has_binding=has_binding,
            has_busy_work=has_busy_work,
            is_chat_bound=is_chat_bound,
        )

        followup = classify_thread_followup(
            {"status": "completed"},
            is_stale=True,
            is_chat_bound=is_chat_bound,
        )
        assert followup["followup_state"] == FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE

    def test_hygiene_protected_covers_binding_and_busy(self) -> None:
        assert is_thread_cleanup_protected(has_binding=True, has_busy_work=True)
        assert is_thread_cleanup_protected(has_binding=True, has_busy_work=False)
        assert is_thread_cleanup_protected(has_binding=False, has_busy_work=True)
        assert not is_thread_cleanup_protected(has_binding=False, has_busy_work=False)

    @pytest.mark.parametrize(
        "status,is_stale,is_chat_bound,expected_followup",
        [
            ("running", False, False, FOLLOWUP_STATE_RUNNING_HEALTHY),
            ("running", True, False, FOLLOWUP_STATE_ATTENTION_REQUIRED),
            ("failed", False, False, FOLLOWUP_STATE_ATTENTION_REQUIRED),
            ("paused", False, False, FOLLOWUP_STATE_AWAITING_FOLLOWUP),
            ("completed", False, False, FOLLOWUP_STATE_REUSABLE),
            ("completed", True, False, FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE),
            ("completed", True, True, FOLLOWUP_STATE_PROTECTED_CHAT_BOUND),
            ("idle", False, False, FOLLOWUP_STATE_REUSABLE),
            ("idle", True, False, FOLLOWUP_STATE_IDLE_ARCHIVE_CANDIDATE),
            ("idle", True, True, FOLLOWUP_STATE_PROTECTED_CHAT_BOUND),
        ],
    )
    def test_followup_state_comprehensive(
        self,
        status: str,
        is_stale: bool,
        is_chat_bound: bool,
        expected_followup: str,
    ) -> None:
        result = classify_thread_followup(
            {"status": status},
            is_stale=is_stale,
            is_chat_bound=is_chat_bound,
        )
        assert result["followup_state"] == expected_followup
