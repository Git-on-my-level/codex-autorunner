"""Chat-core selection helpers over normalized input text."""

from __future__ import annotations

from typing import Any, Dict, List

from .models import ChatContext


class ChatSelectionHandlers:
    """Selection-state helpers shared by platform adapters."""

    _resume_options: Dict[str, Any]
    _bind_options: Dict[str, Any]

    def _selection_page_items(self, state: Any) -> List[tuple[str, str]]:
        """Convert selection state to list of (id, label) tuples."""
        raise NotImplementedError

    def _enqueue_topic_work(self, topic_key: str, work: Any) -> None:
        """Enqueue work for a topic."""
        raise NotImplementedError

    def _resume_thread_by_id(self, topic_key: str, thread_id: str) -> None:
        """Resume a thread by ID."""
        raise NotImplementedError

    def _bind_topic_by_repo_id(self, topic_key: str, repo_id: str) -> None:
        """Bind a topic to a repository ID."""
        raise NotImplementedError

    @staticmethod
    def _selection_belongs_to_user(state: Any, user_id: str | None) -> bool:
        expected: object = getattr(state, "requester_user_id", None)
        if expected is None:
            return True
        return isinstance(expected, str) and expected == user_id

    def handle_pending_resume(self, context: ChatContext, text: str) -> bool:
        if not text.isdigit():
            return False
        state = self._resume_options.get(context.topic_key)
        if not state:
            return False
        if not self._selection_belongs_to_user(state, context.user_id):
            return False
        page_items = self._selection_page_items(state)
        if not page_items:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(page_items):
            return False
        thread_id = page_items[choice - 1][0]
        self._resume_options.pop(context.topic_key, None)
        self._enqueue_topic_work(
            context.topic_key,
            lambda: self._resume_thread_by_id(context.topic_key, thread_id),
        )
        return True

    def handle_pending_bind(self, context: ChatContext, text: str) -> bool:
        if not text.isdigit():
            return False
        state = self._bind_options.get(context.topic_key)
        if not state:
            return False
        if not self._selection_belongs_to_user(state, context.user_id):
            return False
        page_items = self._selection_page_items(state)
        if not page_items:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(page_items):
            return False
        repo_id = page_items[choice - 1][0]
        self._bind_options.pop(context.topic_key, None)
        self._enqueue_topic_work(
            context.topic_key,
            lambda: self._bind_topic_by_repo_id(context.topic_key, repo_id),
        )
        return True
