from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from .interaction_session import (
    DiscordInteractionSession,
    DiscordInteractionSessionManager,
    InteractionSessionError,
    InteractionSessionKind,
)
from .rest import DiscordRestClient


class DiscordResponder:
    def __init__(
        self,
        rest: DiscordRestClient,
        config: Any,
        logger: logging.Logger,
        *,
        hydrate_ack_mode: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
        record_ack: Optional[
            Callable[[str, str, str, Optional[str]], Awaitable[None]]
        ] = None,
        record_delivery: Optional[
            Callable[[str, str, Optional[str], Optional[str]], Awaitable[None]]
        ] = None,
        record_delivery_cursor: Optional[
            Callable[[str, Optional[dict[str, Any]]], Awaitable[None]]
        ] = None,
    ) -> None:
        self._sessions = DiscordInteractionSessionManager(
            rest=rest,
            config=config,
            logger=logger,
        )
        self._hydrate_ack_mode = hydrate_ack_mode
        self._record_ack = record_ack
        self._record_delivery = record_delivery
        self._record_delivery_cursor = record_delivery_cursor

    def start_session(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        kind: InteractionSessionKind,
    ) -> DiscordInteractionSession:
        return self._sessions.start_session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=kind,
        )

    def session(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        kind: InteractionSessionKind = InteractionSessionKind.UNKNOWN,
    ) -> DiscordInteractionSession:
        existing = self.get_session(interaction_token)
        if existing is not None:
            return existing.refresh(interaction_id=interaction_id, kind=kind)
        return self.start_session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=kind,
        )

    def get_session(
        self,
        interaction_token: str,
    ) -> Optional[DiscordInteractionSession]:
        return self._sessions.get_session(interaction_token)

    async def _restore_ack_if_needed(
        self,
        *,
        session: DiscordInteractionSession,
        interaction_id: str,
    ) -> None:
        if session.has_initial_response() or self._hydrate_ack_mode is None:
            return
        ack_mode = await self._hydrate_ack_mode(interaction_id)
        if isinstance(ack_mode, str) and ack_mode.strip():
            session.restore_initial_response(ack_mode)

    async def _ensure_restored_session_state(
        self,
        *,
        session: DiscordInteractionSession,
        interaction_id: str,
    ) -> tuple[bool, bool]:
        had_initial_response = session.has_initial_response()
        if had_initial_response:
            return True, False
        await self._restore_ack_if_needed(
            session=session,
            interaction_id=interaction_id,
        )
        restored_initial_response = session.has_initial_response()
        return restored_initial_response, restored_initial_response

    async def _persist_session_state(
        self,
        *,
        session: DiscordInteractionSession,
        interaction_id: str,
        interaction_token: str,
        had_initial_response: bool,
    ) -> None:
        if (
            not had_initial_response
            and session.has_initial_response()
            and self._record_ack is not None
            and session.initial_response is not None
        ):
            await self._record_ack(
                interaction_id,
                interaction_token,
                session.initial_response.value,
                session.last_delivery_message_id,
            )
        if (
            self._record_delivery is not None
            and session.last_delivery_status is not None
        ):
            await self._record_delivery(
                interaction_id,
                session.last_delivery_status,
                session.last_delivery_error,
                session.last_delivery_message_id,
            )

    @staticmethod
    def _ack_mode_hint_for_operation(operation: str) -> Optional[str]:
        return {
            "respond_message": "immediate_message",
            "respond_with_components": "immediate_message",
            "defer_ephemeral": "defer_ephemeral",
            "defer_public": "defer_public",
            "defer_component_update": "defer_component_update",
            "update_component_message": "component_update",
            "respond_autocomplete": "autocomplete",
            "respond_modal": "modal",
        }.get(operation)

    async def _record_delivery_attempt(
        self,
        interaction_id: str,
        *,
        operation: str,
        payload: dict[str, Any],
        state: str,
        session: Optional[DiscordInteractionSession] = None,
    ) -> None:
        if self._record_delivery_cursor is None:
            return
        if state == "completed":
            await self._clear_delivery_attempt(interaction_id)
            return
        cursor: dict[str, Any] = {
            "state": state,
            "operation": operation,
            "payload": payload,
        }
        ack_mode_hint = self._ack_mode_hint_for_operation(operation)
        if ack_mode_hint is not None:
            cursor["ack_mode_hint"] = ack_mode_hint
        if session is not None:
            if session.last_delivery_status is not None:
                cursor["delivery_status"] = session.last_delivery_status
            if session.last_delivery_error is not None:
                cursor["delivery_error"] = session.last_delivery_error
            if session.last_delivery_message_id is not None:
                cursor["message_id"] = session.last_delivery_message_id
        await self._record_delivery_cursor(interaction_id, cursor)

    async def _clear_delivery_attempt(self, interaction_id: str) -> None:
        if self._record_delivery_cursor is None:
            return
        await self._record_delivery_cursor(interaction_id, None)

    async def respond(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
        )
        payload = {"text": text, "ephemeral": ephemeral}
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_message",
            payload=payload,
            state="pending",
        )
        await session.send_message(text, ephemeral=ephemeral)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_message",
            payload=payload,
            state="completed",
            session=session,
        )

    async def defer(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        ephemeral: bool = False,
    ) -> bool:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        had_initial_response, restored_initial_response = (
            await self._ensure_restored_session_state(
                session=session,
                interaction_id=interaction_id,
            )
        )
        if restored_initial_response:
            return True
        operation = "defer_ephemeral" if ephemeral else "defer_public"
        await self._record_delivery_attempt(
            interaction_id,
            operation=operation,
            payload={"ephemeral": ephemeral},
            state="pending",
        )
        if ephemeral:
            deferred = await session.defer_ephemeral()
        else:
            deferred = await session.defer_public()
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation=operation,
            payload={"ephemeral": ephemeral},
            state="completed" if deferred else "failed",
            session=session,
        )
        return deferred

    async def defer_component_update(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=InteractionSessionKind.COMPONENT,
        )
        had_initial_response, restored_initial_response = (
            await self._ensure_restored_session_state(
                session=session,
                interaction_id=interaction_id,
            )
        )
        if restored_initial_response:
            return True
        payload = {"kind": "component"}
        await self._record_delivery_attempt(
            interaction_id,
            operation="defer_component_update",
            payload=payload,
            state="pending",
        )
        deferred = await session.defer_component_update()
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation="defer_component_update",
            payload=payload,
            state="completed" if deferred else "failed",
            session=session,
        )
        return deferred

    async def send_followup(
        self,
        *,
        interaction_id: Optional[str] = None,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
        ephemeral: bool = False,
    ) -> bool:
        session = self.get_session(interaction_token)
        if session is None:
            if interaction_id is None:
                return False
            session = self.session(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id or session.interaction_id,
        )
        effective_interaction_id = interaction_id or session.interaction_id
        payload = {
            "content": content,
            "components": components,
            "ephemeral": ephemeral,
        }
        await self._record_delivery_attempt(
            effective_interaction_id,
            operation="send_followup",
            payload=payload,
            state="pending",
        )
        try:
            sent = await session.send_followup(
                content=content,
                components=components,
                ephemeral=ephemeral,
            )
        except InteractionSessionError:
            await self._record_delivery_attempt(
                effective_interaction_id,
                operation="send_followup",
                payload=payload,
                state="failed",
                session=session,
            )
            return False
        await self._persist_session_state(
            session=session,
            interaction_id=effective_interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            effective_interaction_id,
            operation="send_followup",
            payload=payload,
            state="completed" if sent else "failed",
            session=session,
        )
        return sent

    async def edit_original_component_message(
        self,
        *,
        interaction_id: Optional[str] = None,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        session = self.get_session(interaction_token)
        if session is None:
            if interaction_id is None:
                return False
            session = self.session(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id or session.interaction_id,
        )
        effective_interaction_id = interaction_id or session.interaction_id
        payload = {"text": text, "components": components}
        await self._record_delivery_attempt(
            effective_interaction_id,
            operation="edit_original_component_message",
            payload=payload,
            state="pending",
        )
        try:
            updated = await session.edit_original_message(
                text=text,
                components=components,
            )
        except InteractionSessionError:
            await self._record_delivery_attempt(
                effective_interaction_id,
                operation="edit_original_component_message",
                payload=payload,
                state="failed",
                session=session,
            )
            return False
        await self._persist_session_state(
            session=session,
            interaction_id=effective_interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            effective_interaction_id,
            operation="edit_original_component_message",
            payload=payload,
            state="completed" if updated else "failed",
            session=session,
        )
        return updated

    async def send_or_respond(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        ephemeral: bool = False,
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
        )
        if deferred and session.has_initial_response():
            if not ephemeral:
                updated = await self.edit_original_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text=text,
                )
                if updated:
                    return
            sent = await self.send_followup(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                content=text,
                ephemeral=ephemeral,
            )
            if sent:
                return
        payload = {"text": text, "ephemeral": ephemeral}
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_message",
            payload=payload,
            state="pending",
        )
        await session.send_message(text, ephemeral=ephemeral)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_message",
            payload=payload,
            state="completed",
            session=session,
        )

    async def send_or_respond_with_components(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
        ephemeral: bool = False,
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
        )
        if deferred and session.has_initial_response():
            if not ephemeral:
                updated = await self.edit_original_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text=text,
                    components=components,
                )
                if updated:
                    return
            sent = await self.send_followup(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                content=text,
                components=components,
                ephemeral=ephemeral,
            )
            if sent:
                return
        payload = {
            "text": text,
            "components": components,
            "ephemeral": ephemeral,
        }
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_with_components",
            payload=payload,
            state="pending",
        )
        await session.send_message(text, ephemeral=ephemeral, components=components)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_with_components",
            payload=payload,
            state="completed",
            session=session,
        )

    async def respond_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
        *,
        ephemeral: bool = False,
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
        )
        payload = {
            "text": text,
            "components": components,
            "ephemeral": ephemeral,
        }
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_with_components",
            payload=payload,
            state="pending",
        )
        await session.send_message(text, ephemeral=ephemeral, components=components)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_with_components",
            payload=payload,
            state="completed",
            session=session,
        )

    async def update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=InteractionSessionKind.COMPONENT,
        )
        had_initial_response, _ = await self._ensure_restored_session_state(
            session=session,
            interaction_id=interaction_id,
        )
        payload = {"text": text, "components": components}
        await self._record_delivery_attempt(
            interaction_id,
            operation="update_component_message",
            payload=payload,
            state="pending",
        )
        await session.update_component_message(text=text, components=components)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation="update_component_message",
            payload=payload,
            state="completed",
            session=session,
        )

    async def respond_autocomplete(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        choices: list[dict[str, str]],
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=InteractionSessionKind.AUTOCOMPLETE,
        )
        had_initial_response = session.has_initial_response()
        payload = {"choices": choices}
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_autocomplete",
            payload=payload,
            state="pending",
        )
        await session.respond_autocomplete(choices=choices)
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_autocomplete",
            payload=payload,
            state="completed",
            session=session,
        )

    async def respond_modal(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        kind: InteractionSessionKind,
        custom_id: str,
        title: str,
        components: list[dict[str, Any]],
    ) -> None:
        session = self.session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=kind,
        )
        had_initial_response = session.has_initial_response()
        payload = {
            "kind": kind.value,
            "custom_id": custom_id,
            "title": title,
            "components": components,
        }
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_modal",
            payload=payload,
            state="pending",
        )
        await session.respond_modal(
            custom_id=custom_id,
            title=title,
            components=components,
        )
        await self._persist_session_state(
            session=session,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            had_initial_response=had_initial_response,
        )
        await self._record_delivery_attempt(
            interaction_id,
            operation="respond_modal",
            payload=payload,
            state="completed",
            session=session,
        )

    async def replay_delivery_cursor(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        cursor: dict[str, Any],
    ) -> bool:
        operation = str(cursor.get("operation") or "").strip()
        payload = cursor.get("payload")
        if not operation or not isinstance(payload, dict):
            return False
        if operation == "send_followup":
            return await self.send_followup(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                content=str(payload.get("content") or ""),
                components=payload.get("components"),
                ephemeral=bool(payload.get("ephemeral", False)),
            )
        if operation == "respond_message":
            await self.respond(
                interaction_id,
                interaction_token,
                str(payload.get("text") or ""),
                ephemeral=bool(payload.get("ephemeral", False)),
            )
            return True
        if operation == "respond_with_components":
            components = payload.get("components")
            if not isinstance(components, list):
                return False
            await self.respond_with_components(
                interaction_id,
                interaction_token,
                str(payload.get("text") or ""),
                components,
                ephemeral=bool(payload.get("ephemeral", False)),
            )
            return True
        if operation == "edit_original_component_message":
            return await self.edit_original_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                text=str(payload.get("text") or ""),
                components=payload.get("components"),
            )
        if operation == "update_component_message":
            components = payload.get("components")
            if not isinstance(components, list):
                return False
            await self.update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                text=str(payload.get("text") or ""),
                components=components,
            )
            return True
        if operation == "defer_ephemeral":
            return await self.defer(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                ephemeral=True,
            )
        if operation == "defer_public":
            return await self.defer(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                ephemeral=False,
            )
        if operation == "defer_component_update":
            return await self.defer_component_update(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        if operation == "respond_autocomplete":
            choices = payload.get("choices")
            if not isinstance(choices, list):
                return False
            await self.respond_autocomplete(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                choices=choices,
            )
            return True
        if operation == "respond_modal":
            components = payload.get("components")
            kind = payload.get("kind")
            if not isinstance(components, list) or not isinstance(kind, str):
                return False
            await self.respond_modal(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                kind=InteractionSessionKind(kind),
                custom_id=str(payload.get("custom_id") or ""),
                title=str(payload.get("title") or ""),
                components=components,
            )
            return True
        return False
