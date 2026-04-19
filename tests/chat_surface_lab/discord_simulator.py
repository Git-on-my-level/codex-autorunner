from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from codex_autorunner.browser.runtime import BrowserRuntime
from codex_autorunner.integrations.discord.errors import (
    DiscordPermanentError,
    DiscordTransientError,
)

from .artifact_manifests import ArtifactManifest
from .evidence_artifacts import write_surface_evidence_artifacts
from .transcript_models import (
    TranscriptEvent,
    TranscriptEventKind,
    TranscriptParty,
    TranscriptTimeline,
)


@dataclass
class DiscordSimulatorFaults:
    """Fault injection knobs for Discord simulator operations."""

    fail_delete_message_ids: set[str] = field(default_factory=set)
    fail_unknown_message_edit_ids: set[str] = field(default_factory=set)
    retry_after_schedule: dict[str, list[int]] = field(default_factory=dict)
    duplicate_interaction_ids: set[str] = field(default_factory=set)


class DiscordSurfaceSimulator:
    """Discord test simulator with semantic timeline + transcript normalization."""

    def __init__(
        self,
        *,
        fail_delete_message_ids: Optional[set[str]] = None,
        faults: Optional[DiscordSimulatorFaults] = None,
        attachment_data_by_url: Optional[dict[str, bytes]] = None,
    ) -> None:
        base_faults = faults or DiscordSimulatorFaults()
        merged_fail_ids = {
            str(value).strip()
            for value in base_faults.fail_delete_message_ids
            if str(value).strip()
        }
        merged_unknown_edit_ids = {
            str(value).strip()
            for value in base_faults.fail_unknown_message_edit_ids
            if str(value).strip()
        }
        if fail_delete_message_ids:
            merged_fail_ids.update(
                str(value).strip() for value in fail_delete_message_ids if str(value)
            )
        base_faults.fail_delete_message_ids = merged_fail_ids
        base_faults.fail_unknown_message_edit_ids = merged_unknown_edit_ids
        self._faults = base_faults
        self.attachment_data_by_url: dict[str, bytes] = {
            str(url): bytes(data)
            for url, data in (attachment_data_by_url or {}).items()
            if str(url)
        }

        # Back-compat fields consumed by existing integration tests.
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_interaction_responses: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.message_ops: list[dict[str, Any]] = []
        self.log_records: list[dict[str, Any]] = []
        self.surface_key: Optional[str] = None
        self.thread_target_id: Optional[str] = None
        self.execution_id: Optional[str] = None
        self.execution_status: Optional[str] = None
        self.execution_error: Optional[str] = None
        self.preview_message_id: Optional[str] = None
        self.preview_deleted: bool = False
        self.terminal_progress_label: Optional[str] = None
        self.background_tasks_drained: bool = False

        self._next_channel_message_id: int = 0
        self._next_followup_message_id: int = 0
        self._surface_timeline: list[dict[str, Any]] = []

    def enable_duplicate_interaction(self, interaction_id: str) -> None:
        normalized = str(interaction_id or "").strip()
        if normalized:
            self._faults.duplicate_interaction_ids.add(normalized)

    def expand_interaction_delivery(
        self,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        interaction_id = str(payload.get("id") or "").strip()
        delivery = [copy.deepcopy(payload)]
        if interaction_id and interaction_id in self._faults.duplicate_interaction_ids:
            delivery.append(copy.deepcopy(payload))
            self._record_event(
                kind="duplicate_interaction_injected",
                party=TranscriptParty.PLATFORM,
                text=f"Injected duplicate interaction {interaction_id}",
                metadata={"interaction_id": interaction_id},
            )
        return delivery

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        self._raise_if_faulted(
            operation="create_interaction_response",
            metadata={"interaction_id": interaction_id},
        )
        response_payload = dict(payload)
        self.interaction_responses.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "payload": response_payload,
            }
        )
        self._record_event(
            kind="ack",
            party=TranscriptParty.PLATFORM,
            text=self._payload_text(response_payload),
            metadata={
                "operation": "create_interaction_response",
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "response_type": response_payload.get("type"),
                "ack_mode": self._ack_mode_from_payload(response_payload),
                "ephemeral": self._payload_is_ephemeral(response_payload),
            },
        )

    async def create_followup_message(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._raise_if_faulted(
            operation="create_followup_message",
            metadata={"interaction_token": interaction_token},
        )
        payload_copy = dict(payload)
        self.followup_messages.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload_copy,
            }
        )
        message_id = self._allocate_followup_id()
        self._record_event(
            kind="send",
            party=TranscriptParty.ASSISTANT,
            text=self._payload_text(payload_copy),
            metadata={
                "operation": "create_followup_message",
                "application_id": application_id,
                "interaction_token": interaction_token,
                "message_id": message_id,
                "ephemeral": self._payload_is_ephemeral(payload_copy),
            },
        )
        return {"id": message_id}

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._raise_if_faulted(
            operation="edit_original_interaction_response",
            metadata={"interaction_token": interaction_token},
        )
        payload_copy = dict(payload)
        self.edited_original_interaction_responses.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload_copy,
            }
        )
        self._record_event(
            kind="edit",
            party=TranscriptParty.ASSISTANT,
            text=self._payload_text(payload_copy),
            metadata={
                "operation": "edit_original_interaction_response",
                "application_id": application_id,
                "interaction_token": interaction_token,
                "message_id": "@original",
            },
        )
        return {"id": "@original"}

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._raise_if_faulted(
            operation="create_channel_message",
            metadata={"channel_id": channel_id},
        )
        message = {"id": self._allocate_channel_message_id()}
        payload_copy = dict(payload)
        self.channel_messages.append(
            {
                "channel_id": channel_id,
                "payload": payload_copy,
                "id": message["id"],
            }
        )
        self.message_ops.append(
            {
                "op": "send",
                "channel_id": channel_id,
                "message_id": message["id"],
                "payload": payload_copy,
            }
        )
        self._record_event(
            kind="send",
            party=TranscriptParty.ASSISTANT,
            text=self._payload_text(payload_copy),
            metadata={
                "operation": "create_channel_message",
                "channel_id": channel_id,
                "message_id": message["id"],
            },
        )
        return message

    async def get_channel_message(
        self, *, channel_id: str, message_id: str
    ) -> dict[str, Any]:
        for op in reversed(self.message_ops):
            if str(op.get("message_id") or "") != message_id:
                continue
            if str(op.get("channel_id") or "") != channel_id:
                continue
            payload = op.get("payload")
            if isinstance(payload, dict):
                return {
                    "id": message_id,
                    "channel_id": channel_id,
                    **payload,
                }
        return {"id": message_id, "channel_id": channel_id}

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._raise_if_faulted(
            operation="edit_channel_message",
            metadata={"channel_id": channel_id, "message_id": message_id},
        )
        if message_id in self._faults.fail_unknown_message_edit_ids:
            error = DiscordPermanentError(
                "Discord API request failed for "
                f"PATCH /channels/{channel_id}/messages/{message_id}: "
                'status=404 body=\'{"message": "Unknown Message", "code": 10008}\''
            )
            self._record_event(
                kind="error",
                party=TranscriptParty.PLATFORM,
                text=str(error),
                metadata={
                    "operation": "edit_channel_message",
                    "fault": "unknown_message",
                    "channel_id": channel_id,
                    "message_id": message_id,
                },
            )
            raise error
        payload_copy = dict(payload)
        self.edited_channel_messages.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": payload_copy,
            }
        )
        self.message_ops.append(
            {
                "op": "edit",
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": payload_copy,
            }
        )
        self._record_event(
            kind="edit",
            party=TranscriptParty.ASSISTANT,
            text=self._payload_text(payload_copy),
            metadata={
                "operation": "edit_channel_message",
                "channel_id": channel_id,
                "message_id": message_id,
            },
        )
        return {"id": message_id}

    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        self._raise_if_faulted(
            operation="delete_channel_message",
            metadata={"channel_id": channel_id, "message_id": message_id},
        )
        if message_id in self._faults.fail_delete_message_ids:
            error = RuntimeError(f"delete failed for {message_id}")
            self._record_event(
                kind="error",
                party=TranscriptParty.PLATFORM,
                text=str(error),
                metadata={
                    "operation": "delete_channel_message",
                    "fault": "delete_failed",
                    "channel_id": channel_id,
                    "message_id": message_id,
                },
            )
            raise error
        self.deleted_channel_messages.append(
            {"channel_id": channel_id, "message_id": message_id}
        )
        self.message_ops.append(
            {
                "op": "delete",
                "channel_id": channel_id,
                "message_id": message_id,
            }
        )
        self._record_event(
            kind="delete",
            party=TranscriptParty.PLATFORM,
            text="",
            metadata={
                "operation": "delete_channel_message",
                "channel_id": channel_id,
                "message_id": message_id,
            },
        )

    async def download_attachment(
        self, *, url: str, max_size_bytes: Optional[int] = None
    ) -> bytes:
        normalized_url = str(url or "").strip()
        if normalized_url not in self.attachment_data_by_url:
            raise RuntimeError(f"no attachment fixture for {normalized_url}")
        data = self.attachment_data_by_url[normalized_url]
        if max_size_bytes is not None and len(data) > max_size_bytes:
            raise RuntimeError(
                f"attachment exceeds max size ({len(data)} > {max_size_bytes})"
            )
        self._record_event(
            kind="status",
            party=TranscriptParty.PLATFORM,
            text="download_attachment",
            metadata={
                "operation": "download_attachment",
                "url": normalized_url,
                "size_bytes": len(data),
            },
        )
        return data

    async def trigger_typing(self, *, channel_id: str) -> None:
        self._raise_if_faulted(
            operation="trigger_typing",
            metadata={"channel_id": channel_id},
        )
        self.typing_calls.append(channel_id)
        self._record_event(
            kind="status",
            party=TranscriptParty.PLATFORM,
            text="typing",
            metadata={"operation": "trigger_typing", "channel_id": channel_id},
        )

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self._raise_if_faulted(
            operation="bulk_overwrite_application_commands",
            metadata={"application_id": application_id, "guild_id": guild_id},
        )
        self._record_event(
            kind="status",
            party=TranscriptParty.PLATFORM,
            text="bulk_overwrite_application_commands",
            metadata={
                "operation": "bulk_overwrite_application_commands",
                "application_id": application_id,
                "guild_id": guild_id,
                "command_count": len(commands),
            },
        )
        return commands

    @property
    def surface_timeline(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(item) for item in self._surface_timeline)

    def to_normalized_transcript(
        self,
        *,
        scenario_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> TranscriptTimeline:
        events: list[TranscriptEvent] = []
        for item in self._surface_timeline:
            kind_value = str(item.get("kind") or "status")
            kind_map = {
                "ack": TranscriptEventKind.ACK,
                "send": TranscriptEventKind.SEND,
                "edit": TranscriptEventKind.EDIT,
                "delete": TranscriptEventKind.DELETE,
                "error": TranscriptEventKind.ERROR,
                "status": TranscriptEventKind.STATUS,
                "duplicate_interaction_injected": TranscriptEventKind.STATUS,
            }
            transcript_kind = kind_map.get(kind_value, TranscriptEventKind.STATUS)
            party_raw = str(item.get("party") or TranscriptParty.PLATFORM.value)
            try:
                party = TranscriptParty(party_raw)
            except ValueError:
                party = TranscriptParty.PLATFORM
            item_metadata = item.get("metadata")
            events.append(
                TranscriptEvent(
                    kind=transcript_kind,
                    party=party,
                    timestamp_ms=int(item.get("timestamp_ms") or 0),
                    surface_kind="discord",
                    text=str(item.get("text") or ""),
                    metadata=(
                        dict(item_metadata) if isinstance(item_metadata, dict) else {}
                    ),
                )
            )
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("surface_kind", "discord")
        merged_metadata.setdefault("event_count", str(len(events)))
        return TranscriptTimeline(
            scenario_id=scenario_id,
            events=tuple(events),
            metadata=merged_metadata,
        )

    def write_artifacts(
        self,
        *,
        output_dir: Path,
        scenario_id: str,
        run_id: str,
        browser_runtime: Optional[BrowserRuntime] = None,
    ) -> ArtifactManifest:
        transcript = self.to_normalized_transcript(scenario_id=scenario_id)
        return write_surface_evidence_artifacts(
            output_dir=output_dir,
            scenario_id=scenario_id,
            run_id=run_id,
            surface_kind="discord",
            surface_timeline=self._surface_timeline,
            transcript=transcript,
            log_records=self.log_records,
            browser_runtime=browser_runtime,
        )

    def _raise_if_faulted(
        self,
        *,
        operation: str,
        metadata: dict[str, Any],
    ) -> None:
        retry_after = self._pop_retry_after(operation)
        if retry_after is None:
            return
        error = DiscordTransientError(
            f"Discord simulator rate limited {operation}; retry after {retry_after}s",
            retry_after=retry_after,
        )
        event_metadata = dict(metadata)
        event_metadata["operation"] = operation
        event_metadata["fault"] = "retry_after"
        event_metadata["retry_after"] = retry_after
        self._record_event(
            kind="error",
            party=TranscriptParty.PLATFORM,
            text=str(error),
            metadata=event_metadata,
        )
        raise error

    def _pop_retry_after(self, operation: str) -> Optional[int]:
        queue = self._faults.retry_after_schedule.get(operation)
        if not queue:
            return None
        value = queue.pop(0)
        try:
            retry_after = int(value)
        except (TypeError, ValueError):
            return None
        return retry_after if retry_after > 0 else None

    def _allocate_channel_message_id(self) -> str:
        self._next_channel_message_id += 1
        return f"msg-{self._next_channel_message_id}"

    def _allocate_followup_id(self) -> str:
        self._next_followup_message_id += 1
        return f"followup-{self._next_followup_message_id}"

    @staticmethod
    def _payload_text(payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if isinstance(content, str):
            return content
        data = payload.get("data")
        if isinstance(data, dict):
            nested_content = data.get("content")
            if isinstance(nested_content, str):
                return nested_content
        return ""

    @staticmethod
    def _payload_is_ephemeral(payload: dict[str, Any]) -> bool:
        flags = payload.get("flags")
        if isinstance(flags, int):
            return bool(flags & 64)
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("flags"), int):
            return bool(int(data["flags"]) & 64)
        return False

    @classmethod
    def _ack_mode_from_payload(cls, payload: dict[str, Any]) -> str:
        response_type = payload.get("type")
        if response_type == 4:
            return (
                "immediate_ephemeral"
                if cls._payload_is_ephemeral(payload)
                else "immediate_public"
            )
        if response_type == 5:
            return (
                "defer_ephemeral"
                if cls._payload_is_ephemeral(payload)
                else "defer_public"
            )
        if response_type == 6:
            return "defer_component_update"
        if response_type == 7:
            return "component_update"
        if response_type == 8:
            return "autocomplete"
        if response_type == 9:
            return "modal"
        return "unknown"

    def _record_event(
        self,
        *,
        kind: str,
        party: TranscriptParty,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        self._surface_timeline.append(
            {
                "timestamp_ms": int(time.time() * 1000),
                "kind": kind,
                "party": party.value,
                "text": text,
                "metadata": dict(metadata),
            }
        )


__all__ = [
    "DiscordSimulatorFaults",
    "DiscordSurfaceSimulator",
]
