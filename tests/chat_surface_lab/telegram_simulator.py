from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from codex_autorunner.browser.runtime import BrowserRuntime
from codex_autorunner.integrations.telegram.adapter import (
    TelegramAPIError,
    TelegramUpdate,
    chunk_message,
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
class TelegramSimulatorFaults:
    """Fault injection knobs for Telegram simulator send/edit/delete behaviors."""

    fail_delete_message_ids: set[int] = field(default_factory=set)
    retry_after_schedule: dict[str, list[int]] = field(default_factory=dict)
    parse_mode_rejections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    duplicate_update_ids: set[int] = field(default_factory=set)


class TelegramSurfaceSimulator:
    """Telegram test simulator with semantic timeline + transcript normalization."""

    def __init__(
        self,
        *,
        fail_delete_message_ids: Optional[set[int]] = None,
        faults: Optional[TelegramSimulatorFaults] = None,
    ) -> None:
        base_faults = faults or TelegramSimulatorFaults()
        merged_fail_ids = set(base_faults.fail_delete_message_ids)
        if fail_delete_message_ids:
            merged_fail_ids.update(int(value) for value in fail_delete_message_ids)
        base_faults.fail_delete_message_ids = merged_fail_ids
        self._faults = base_faults

        # Back-compat fields consumed by existing integration tests.
        self.messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.deleted_messages: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self.log_records: list[dict[str, Any]] = []
        self.surface_key: Optional[str] = None
        self.thread_target_id: Optional[str] = None
        self.execution_id: Optional[str] = None
        self.execution_status: Optional[str] = None
        self.execution_error: Optional[str] = None
        self.placeholder_message_id: Optional[int] = None
        self.placeholder_deleted: bool = False
        self.background_tasks_drained: bool = False

        self._next_message_id: int = 0
        self._surface_timeline: list[dict[str, Any]] = []

    def enable_duplicate_update(self, update_id: int) -> None:
        self._faults.duplicate_update_ids.add(int(update_id))

    def expand_update_delivery(self, update: TelegramUpdate) -> list[TelegramUpdate]:
        delivery = [update]
        should_duplicate = update.update_id in self._faults.duplicate_update_ids
        if should_duplicate:
            delivery.append(update)
            self._record_event(
                kind="duplicate_update_injected",
                party=TranscriptParty.PLATFORM,
                text=f"Injected duplicate update {update.update_id}",
                metadata={"update_id": update.update_id},
            )
        return delivery

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        _ = disable_web_page_preview
        self._raise_if_faulted(
            operation="send_message",
            text=text,
            parse_mode=parse_mode,
        )
        message_id = self._allocate_message_id()
        record = {
            "chat_id": chat_id,
            "thread_id": message_thread_id,
            "reply_to": reply_to_message_id,
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
            "message_id": message_id,
        }
        self.messages.append(record)
        self._record_event(
            kind="send",
            party=TranscriptParty.ASSISTANT,
            text=text,
            metadata={
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "topic_key": self._topic_key(chat_id, message_thread_id),
                "reply_to_message_id": reply_to_message_id,
                "parse_mode": parse_mode,
                "message_id": message_id,
            },
        )
        return {"message_id": message_id}

    async def send_message_chunks(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
        max_len: int = 4096,
    ) -> list[dict[str, Any]]:
        chunks = chunk_message(text, max_len=max_len, with_numbering=False)
        responses: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            response = await self.send_message(
                chat_id,
                chunk,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id if index == 0 else None,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
                reply_markup=reply_markup if index == 0 else None,
            )
            message_id = response.get("message_id")
            if isinstance(message_id, int):
                self.messages[-1]["chunk_index"] = index
                self.messages[-1]["chunk_total"] = len(chunks)
            responses.append(response)
        if len(chunks) > 1:
            self._record_event(
                kind="chunking",
                party=TranscriptParty.PLATFORM,
                text=f"Sent {len(chunks)} chunks",
                metadata={
                    "chat_id": chat_id,
                    "thread_id": message_thread_id,
                    "topic_key": self._topic_key(chat_id, message_thread_id),
                    "chunk_count": len(chunks),
                    "text_len": len(text),
                    "parse_mode": parse_mode,
                },
            )
        return responses

    async def send_document(
        self,
        chat_id: int,
        document: bytes,
        *,
        filename: str,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        self._raise_if_faulted(
            operation="send_document",
            text=caption or "",
            parse_mode=parse_mode,
        )
        message_id = self._allocate_message_id()
        record = {
            "chat_id": chat_id,
            "thread_id": message_thread_id,
            "reply_to": reply_to_message_id,
            "filename": filename,
            "caption": caption,
            "bytes_len": len(document),
            "parse_mode": parse_mode,
            "message_id": message_id,
        }
        self.documents.append(record)
        self._record_event(
            kind="attachment",
            party=TranscriptParty.ASSISTANT,
            text=caption or "",
            metadata={
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "topic_key": self._topic_key(chat_id, message_thread_id),
                "reply_to_message_id": reply_to_message_id,
                "filename": filename,
                "bytes_len": len(document),
                "parse_mode": parse_mode,
                "message_id": message_id,
            },
        )
        return {"message_id": message_id}

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        chat_id: Optional[int] = None,
        thread_id: Optional[int] = None,
        message_id: Optional[int] = None,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        self._raise_if_faulted(
            operation="answer_callback_query",
            text=text or "",
            parse_mode=None,
        )
        record = {
            "callback_query_id": callback_query_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "text": text,
            "show_alert": show_alert,
        }
        self.callback_answers.append(record)
        self._record_event(
            kind="callback",
            party=TranscriptParty.PLATFORM,
            text=text or "",
            metadata=record,
        )
        return {}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        _ = disable_web_page_preview
        self._raise_if_faulted(
            operation="edit_message_text",
            text=text,
            parse_mode=parse_mode,
        )
        record = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        }
        self.edited_messages.append(record)
        self._record_event(
            kind="edit",
            party=TranscriptParty.ASSISTANT,
            text=text,
            metadata=record,
        )
        return {"message_id": message_id}

    async def delete_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        self._raise_if_faulted(
            operation="delete_message",
            text="",
            parse_mode=None,
        )
        if message_id in self._faults.fail_delete_message_ids:
            error = RuntimeError(f"delete failed for {message_id}")
            self._record_event(
                kind="error",
                party=TranscriptParty.PLATFORM,
                text=str(error),
                metadata={
                    "operation": "delete_message",
                    "chat_id": chat_id,
                    "thread_id": message_thread_id,
                    "message_id": message_id,
                },
            )
            raise error
        record = {
            "chat_id": chat_id,
            "thread_id": message_thread_id,
            "message_id": message_id,
        }
        self.deleted_messages.append(record)
        self._record_event(
            kind="delete",
            party=TranscriptParty.PLATFORM,
            text="",
            metadata=record,
        )
        return True

    @property
    def surface_timeline(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._surface_timeline)

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
                "send": TranscriptEventKind.SEND,
                "edit": TranscriptEventKind.EDIT,
                "delete": TranscriptEventKind.DELETE,
                "attachment": TranscriptEventKind.ATTACHMENT,
                "callback": TranscriptEventKind.CALLBACK,
                "error": TranscriptEventKind.ERROR,
                "chunking": TranscriptEventKind.STATUS,
                "duplicate_update_injected": TranscriptEventKind.STATUS,
            }
            transcript_kind = kind_map.get(kind_value, TranscriptEventKind.STATUS)
            party_raw = str(item.get("party") or TranscriptParty.PLATFORM.value)
            try:
                party = TranscriptParty(party_raw)
            except ValueError:
                party = TranscriptParty.PLATFORM
            timestamp_ms = int(item.get("timestamp_ms") or 0)
            text = str(item.get("text") or "")
            item_metadata = item.get("metadata")
            events.append(
                TranscriptEvent(
                    kind=transcript_kind,
                    party=party,
                    timestamp_ms=timestamp_ms,
                    surface_kind="telegram",
                    text=text,
                    metadata=(
                        dict(item_metadata) if isinstance(item_metadata, dict) else {}
                    ),
                )
            )
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("surface_kind", "telegram")
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
            surface_kind="telegram",
            surface_timeline=self._surface_timeline,
            transcript=transcript,
            log_records=self.log_records,
            browser_runtime=browser_runtime,
        )

    def _allocate_message_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    def _topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id if thread_id is not None else 'root'}"

    def _raise_if_faulted(
        self,
        *,
        operation: str,
        text: str,
        parse_mode: Optional[str],
    ) -> None:
        retry_after = self._pop_retry_after(operation)
        if retry_after is not None:
            error = TelegramAPIError(
                f"Too Many Requests: retry after {retry_after}",
                retry_after=retry_after,
            )
            self._record_event(
                kind="error",
                party=TranscriptParty.PLATFORM,
                text=str(error),
                metadata={
                    "operation": operation,
                    "fault": "retry_after",
                    "retry_after": retry_after,
                },
            )
            raise error
        if (
            parse_mode
            and parse_mode in self._faults.parse_mode_rejections
            and self._faults.parse_mode_rejections[parse_mode]
        ):
            for token in self._faults.parse_mode_rejections[parse_mode]:
                if token and token in text:
                    error = TelegramAPIError(
                        f"Bad Request: can't parse entities in {parse_mode}",
                        user_message="Telegram API error.",
                    )
                    self._record_event(
                        kind="error",
                        party=TranscriptParty.PLATFORM,
                        text=str(error),
                        metadata={
                            "operation": operation,
                            "fault": "parse_mode_rejected",
                            "parse_mode": parse_mode,
                            "token": token,
                        },
                    )
                    raise error

    def _pop_retry_after(self, operation: str) -> Optional[int]:
        scheduled = self._faults.retry_after_schedule.get(operation)
        if not scheduled:
            return None
        value = scheduled.pop(0)
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return None
        return numeric if numeric > 0 else None

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
    "TelegramSimulatorFaults",
    "TelegramSurfaceSimulator",
]
