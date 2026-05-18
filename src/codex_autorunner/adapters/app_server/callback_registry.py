from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional, Union

from .ids import extract_thread_id_for_turn, extract_turn_id
from .protocol_helpers import (
    RawApprovalRequestAdapter,
    RawNotificationAdapter,
    RawUserInputRequestAdapter,
)
from .turn_state import TurnKey

ApprovalDecision = Union[str, dict[str, Any]]
ApprovalHandler = Callable[[dict[str, Any]], Awaitable[ApprovalDecision]]
UserInputResponse = dict[str, Any]
UserInputHandler = Callable[[dict[str, Any]], Awaitable[UserInputResponse]]
NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RuntimeCallbackRegistry:
    """Resolve app-server callbacks by turn, then thread, then global handler."""

    def __init__(
        self,
        *,
        approval_handler: Optional[ApprovalHandler],
        question_handler: Optional[UserInputHandler],
        notification_handler: Optional[NotificationHandler],
        default_approval_decision: str,
        default_user_input_result: Callable[[Any], dict[str, Any]],
    ) -> None:
        self._approval_handler = approval_handler
        self._question_handler = question_handler
        self._notification_handler = notification_handler
        self._default_approval_decision = default_approval_decision
        self._default_user_input_result = default_user_input_result
        self._thread_approval_handlers: dict[str, ApprovalHandler] = {}
        self._turn_approval_handlers: dict[TurnKey, ApprovalHandler] = {}
        self._thread_user_input_handlers: dict[str, UserInputHandler] = {}
        self._turn_user_input_handlers: dict[TurnKey, UserInputHandler] = {}
        self._thread_notification_handlers: dict[str, NotificationHandler] = {}
        self._turn_notification_handlers: dict[TurnKey, NotificationHandler] = {}

    @property
    def notification_handler(self) -> Optional[NotificationHandler]:
        return self._notification_handler

    def configure(
        self,
        *,
        approval_handler: Optional[ApprovalHandler] = None,
        question_handler: Optional[UserInputHandler] = None,
        notification_handler: Optional[NotificationHandler] = None,
        default_approval_decision: Optional[str] = None,
    ) -> None:
        self._approval_handler = approval_handler
        self._question_handler = question_handler
        self._notification_handler = notification_handler
        if (
            isinstance(default_approval_decision, str)
            and default_approval_decision.strip()
        ):
            self._default_approval_decision = default_approval_decision.strip()

    def register(
        self,
        *,
        thread_id: str,
        turn_id: Optional[str] = None,
        approval_handler: Optional[ApprovalHandler] = None,
        question_handler: Optional[UserInputHandler] = None,
        notification_handler: Optional[NotificationHandler] = None,
    ) -> None:
        if not isinstance(thread_id, str) or not thread_id.strip():
            return
        thread_key = thread_id.strip()
        if approval_handler is not None:
            self._thread_approval_handlers[thread_key] = approval_handler
        if question_handler is not None:
            self._thread_user_input_handlers[thread_key] = question_handler
        if notification_handler is not None:
            self._thread_notification_handlers[thread_key] = notification_handler
        if isinstance(turn_id, str) and turn_id.strip():
            turn_key = (thread_key, turn_id.strip())
            if approval_handler is not None:
                self._turn_approval_handlers[turn_key] = approval_handler
            if question_handler is not None:
                self._turn_user_input_handlers[turn_key] = question_handler
            if notification_handler is not None:
                self._turn_notification_handlers[turn_key] = notification_handler

    def unregister(
        self, *, thread_id: Optional[str] = None, turn_id: Optional[str] = None
    ) -> None:
        if not isinstance(thread_id, str) or not thread_id.strip():
            return
        thread_key = thread_id.strip()
        self._thread_approval_handlers.pop(thread_key, None)
        self._thread_user_input_handlers.pop(thread_key, None)
        self._thread_notification_handlers.pop(thread_key, None)
        if isinstance(turn_id, str) and turn_id.strip():
            turn_key = (thread_key, turn_id.strip())
            self._turn_approval_handlers.pop(turn_key, None)
            self._turn_user_input_handlers.pop(turn_key, None)
            self._turn_notification_handlers.pop(turn_key, None)
            return
        for handler_map in (
            self._turn_approval_handlers,
            self._turn_user_input_handlers,
            self._turn_notification_handlers,
        ):
            for key in tuple(handler_map):
                if key[0] == thread_key:
                    handler_map.pop(key, None)

    def request_route_ids(
        self, params: dict[str, Any], decoded: Any = None
    ) -> tuple[Optional[str], Optional[str]]:
        turn_id = (
            getattr(decoded, "turn_id", None)
            or extract_turn_id(params)
            or extract_turn_id(params.get("item"))
        )
        thread_id = (
            getattr(decoded, "thread_id", None)
            or extract_thread_id_for_turn(params)
            or params.get("threadId")
            or params.get("thread_id")
        )
        if isinstance(thread_id, str):
            thread_id = thread_id.strip() or None
        else:
            thread_id = None
        if isinstance(turn_id, str):
            turn_id = turn_id.strip() or None
        else:
            turn_id = None
        return thread_id, turn_id

    def approval_adapter_for(self, envelope: Any) -> RawApprovalRequestAdapter:
        thread_id, turn_id = self.request_route_ids(
            envelope.params,
            getattr(envelope, "request", None),
        )
        handler: Optional[ApprovalHandler] = None
        if thread_id and turn_id:
            handler = self._turn_approval_handlers.get((thread_id, turn_id))
        if handler is None and thread_id:
            handler = self._thread_approval_handlers.get(thread_id)
        if handler is None:
            handler = self._approval_handler
        return RawApprovalRequestAdapter(
            handler, default_decision=self._default_approval_decision
        )

    def user_input_adapter_for(self, envelope: Any) -> RawUserInputRequestAdapter:
        thread_id, turn_id = self.request_route_ids(
            envelope.params,
            getattr(envelope, "request", None),
        )
        handler: Optional[UserInputHandler] = None
        if thread_id and turn_id:
            handler = self._turn_user_input_handlers.get((thread_id, turn_id))
        if handler is None and thread_id:
            handler = self._thread_user_input_handlers.get(thread_id)
        if handler is None:
            handler = self._question_handler
        return RawUserInputRequestAdapter(
            handler,
            default_result_factory=self._default_user_input_result,
        )

    def notification_adapter_for(
        self, envelope: Any
    ) -> Optional[RawNotificationAdapter]:
        thread_id, turn_id = self.request_route_ids(
            envelope.params,
            getattr(envelope, "notification", None),
        )
        handler: Optional[NotificationHandler] = None
        if thread_id and turn_id:
            handler = self._turn_notification_handlers.get((thread_id, turn_id))
        if handler is None and thread_id:
            handler = self._thread_notification_handlers.get(thread_id)
        if handler is None:
            handler = self._notification_handler
        if handler is None:
            return None
        return RawNotificationAdapter(handler)

    def has_thread_callbacks(self, thread_id: str) -> bool:
        return (
            thread_id in self._thread_approval_handlers
            or thread_id in self._thread_user_input_handlers
            or thread_id in self._thread_notification_handlers
        )
