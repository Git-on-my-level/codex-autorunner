from __future__ import annotations

from typing import Any, Optional

from .types import (
    CompactState,
    DocumentBrowserState,
    ModelPendingState,
    ModelPickerState,
    PendingQuestion,
    ReviewCommitSelectionState,
    SelectionState,
    UpdateConfirmState,
)


class TelegramUiState:
    """Own Telegram-local ephemeral UI state and its cleanup rules."""

    def __init__(self) -> None:
        self.pending_questions: dict[str, PendingQuestion] = {}
        self.resume_options: dict[str, SelectionState] = {}
        self.bind_options: dict[str, SelectionState] = {}
        self.flow_run_options: dict[str, SelectionState] = {}
        self.update_options: dict[str, SelectionState] = {}
        self.update_confirm_options: dict[str, UpdateConfirmState] = {}
        self.review_commit_options: dict[str, ReviewCommitSelectionState] = {}
        self.review_commit_subjects: dict[str, dict[str, str]] = {}
        self.pending_review_custom: dict[str, dict[str, Any]] = {}
        self.compact_pending: dict[str, CompactState] = {}
        self.agent_options: dict[str, SelectionState] = {}
        self.agent_profile_options: dict[str, SelectionState] = {}
        self.model_options: dict[str, ModelPickerState] = {}
        self.model_pending: dict[str, ModelPendingState] = {}
        self.document_browser_states: dict[str, DocumentBrowserState] = {}

    @staticmethod
    def owner_matches(state: Any, actor_id: Optional[str]) -> bool:
        expected = getattr(state, "requester_user_id", None)
        if expected is None:
            return True
        if actor_id is None:
            return False
        return expected == actor_id

    def pop_if_owned(
        self, state_map: dict[str, Any], key: str, actor_id: Optional[str]
    ) -> Any:
        state = state_map.get(key)
        if not self.owner_matches(state, actor_id):
            return None
        return state_map.pop(key, None)

    def clear_pending_options(
        self, key: str, actor_id: Optional[str]
    ) -> dict[str, Any] | None:
        self.pop_if_owned(self.resume_options, key, actor_id)
        self.pop_if_owned(self.bind_options, key, actor_id)
        self.pop_if_owned(self.flow_run_options, key, actor_id)
        self.pop_if_owned(self.agent_options, key, actor_id)
        self.pop_if_owned(self.agent_profile_options, key, actor_id)
        self.pop_if_owned(self.update_options, key, actor_id)
        self.pop_if_owned(self.update_confirm_options, key, actor_id)
        self.pop_if_owned(self.compact_pending, key, actor_id)
        self.pop_if_owned(self.model_options, key, actor_id)
        self.pop_if_owned(self.model_pending, key, actor_id)
        self.pop_if_owned(self.document_browser_states, key, actor_id)
        review_state = self.pop_if_owned(self.review_commit_options, key, actor_id)
        if review_state is not None:
            self.review_commit_subjects.pop(key, None)
        return self.pop_if_owned(self.pending_review_custom, key, actor_id)

    def has_policy_blocking_state(self, key: str) -> bool:
        return bool(
            self.resume_options.get(key)
            or self.bind_options.get(key)
            or self.review_commit_options.get(key)
            or self.pending_review_custom.get(key)
        )

    def clear_for_bang_command(self, key: str, actor_id: Optional[str]) -> None:
        self.pop_if_owned(self.resume_options, key, actor_id)
        self.pop_if_owned(self.bind_options, key, actor_id)
        self.pop_if_owned(self.flow_run_options, key, actor_id)
        self.pop_if_owned(self.agent_options, key, actor_id)
        self.pop_if_owned(self.agent_profile_options, key, actor_id)
        self.pop_if_owned(self.model_options, key, actor_id)
        self.pop_if_owned(self.model_pending, key, actor_id)
        self.pop_if_owned(self.document_browser_states, key, actor_id)

    def clear_for_command(
        self, key: str, actor_id: Optional[str], command_name: str
    ) -> dict[str, Any] | None:
        if command_name != "resume":
            self.pop_if_owned(self.resume_options, key, actor_id)
        if command_name != "bind":
            self.pop_if_owned(self.bind_options, key, actor_id)
        if command_name != "flow":
            self.pop_if_owned(self.flow_run_options, key, actor_id)
        if command_name != "agent":
            self.pop_if_owned(self.agent_options, key, actor_id)
            self.pop_if_owned(self.agent_profile_options, key, actor_id)
        if command_name != "model":
            self.pop_if_owned(self.model_options, key, actor_id)
            self.pop_if_owned(self.model_pending, key, actor_id)
        if command_name != "update":
            self.pop_if_owned(self.update_options, key, actor_id)
            self.pop_if_owned(self.update_confirm_options, key, actor_id)
        if command_name not in {"tickets", "contextspace"}:
            self.pop_if_owned(self.document_browser_states, key, actor_id)
        self.pop_if_owned(self.compact_pending, key, actor_id)
        if command_name == "review":
            return None
        review_state = self.pop_if_owned(self.review_commit_options, key, actor_id)
        if review_state is not None:
            self.review_commit_subjects.pop(key, None)
        return self.pop_if_owned(self.pending_review_custom, key, actor_id)

    def clear_for_message(
        self, key: str, actor_id: Optional[str]
    ) -> dict[str, Any] | None:
        self.pop_if_owned(self.resume_options, key, actor_id)
        self.pop_if_owned(self.bind_options, key, actor_id)
        self.pop_if_owned(self.flow_run_options, key, actor_id)
        self.pop_if_owned(self.agent_options, key, actor_id)
        self.pop_if_owned(self.agent_profile_options, key, actor_id)
        self.pop_if_owned(self.update_options, key, actor_id)
        self.pop_if_owned(self.update_confirm_options, key, actor_id)
        self.pop_if_owned(self.compact_pending, key, actor_id)
        self.pop_if_owned(self.model_options, key, actor_id)
        self.pop_if_owned(self.model_pending, key, actor_id)
        self.pop_if_owned(self.document_browser_states, key, actor_id)
        review_state = self.pop_if_owned(self.review_commit_options, key, actor_id)
        if review_state is not None:
            self.review_commit_subjects.pop(key, None)
        return self.pop_if_owned(self.pending_review_custom, key, actor_id)

    def evict_cache_entry(self, cache_name: str, key: object) -> bool:
        if cache_name == "resume_options":
            self.resume_options.pop(str(key), None)
            return True
        if cache_name == "bind_options":
            self.bind_options.pop(str(key), None)
            return True
        if cache_name == "flow_run_options":
            self.flow_run_options.pop(str(key), None)
            return True
        if cache_name == "agent_options":
            self.agent_options.pop(str(key), None)
            return True
        if cache_name == "agent_profile_options":
            self.agent_profile_options.pop(str(key), None)
            return True
        if cache_name == "update_options":
            self.update_options.pop(str(key), None)
            return True
        if cache_name == "update_confirm_options":
            self.update_confirm_options.pop(str(key), None)
            return True
        if cache_name == "review_commit_options":
            self.review_commit_options.pop(str(key), None)
            return True
        if cache_name == "review_commit_subjects":
            self.review_commit_subjects.pop(str(key), None)
            return True
        if cache_name == "pending_review_custom":
            self.pending_review_custom.pop(str(key), None)
            return True
        if cache_name == "compact_pending":
            self.compact_pending.pop(str(key), None)
            return True
        if cache_name == "model_options":
            self.model_options.pop(str(key), None)
            return True
        if cache_name == "model_pending":
            self.model_pending.pop(str(key), None)
            return True
        if cache_name == "document_browser_states":
            self.document_browser_states.pop(str(key), None)
            return True
        if cache_name == "pending_questions":
            self.pending_questions.pop(str(key), None)
            return True
        return False
