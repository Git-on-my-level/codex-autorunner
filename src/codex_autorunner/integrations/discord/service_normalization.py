from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ...core.flows.ux_helpers import summarize_flow_freshness
from ...core.injected_context import wrap_injected_context
from ...core.ticket_flow_summary import build_ticket_flow_display
from ...integrations.chat.media import (
    audio_content_type_for_input,
    audio_extension_for_input,
    is_audio_mime_or_path,
    is_image_mime_or_path,
    normalize_mime_type,
)
from ...integrations.chat.update_notifier import format_update_status_message
from .components import (
    DISCORD_BUTTON_STYLE_SUCCESS,
    build_action_row,
    build_button,
    build_queue_notice_buttons,
    build_queue_status_buttons,
)
from .rendering import format_discord_message, truncate_for_discord


def _clean_optional_text(value: object) -> Optional[str]:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _clean_optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


@dataclass(frozen=True)
class DiscordAttachmentAdapter:
    file_id: Optional[str]
    file_name: Optional[str]
    mime_type: Optional[str]
    source_url: Optional[str]
    kind: Optional[str]
    size_bytes: Optional[int]

    @classmethod
    def from_raw(cls, raw: object) -> "DiscordAttachmentAdapter":
        return cls(
            file_id=_clean_optional_text(getattr(raw, "file_id", None)),
            file_name=_clean_optional_text(getattr(raw, "file_name", None)),
            mime_type=_clean_optional_text(getattr(raw, "mime_type", None)),
            source_url=_clean_optional_text(getattr(raw, "source_url", None)),
            kind=_clean_optional_text(getattr(raw, "kind", None)),
            size_bytes=_clean_optional_int(getattr(raw, "size_bytes", None)),
        )

    def is_audio(self, *, mime_type: Optional[str] = None) -> bool:
        return is_audio_mime_or_path(
            mime_type=mime_type if mime_type is not None else self.mime_type,
            file_name=self.file_name,
            source_url=self.source_url,
            kind=self.kind,
        )

    def is_image(self, *, original_name: Optional[str] = None) -> bool:
        return is_image_mime_or_path(
            self.mime_type,
            str(original_name or self.file_name or ""),
        )

    def transcription_filename(
        self,
        *,
        saved_name: str,
        mime_type: Optional[str] = None,
    ) -> str:
        if not self.file_name:
            return saved_name
        candidate = Path(self.file_name).name.strip()
        if not candidate:
            return saved_name
        if not self.is_audio(mime_type=mime_type):
            return candidate
        if audio_content_type_for_input(
            mime_type=None,
            file_name=candidate,
            source_url=None,
        ):
            return candidate
        return saved_name

    def build_saved_name(self, *, index: int, token: str) -> str:
        raw_name = self.file_name or f"attachment-{index}"
        base_name = Path(str(raw_name)).name.strip()
        if not base_name or base_name in {".", ".."}:
            base_name = f"attachment-{index}"
        safe_name = "".join(
            ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in base_name
        ).strip("._")
        if not safe_name:
            safe_name = f"attachment-{index}"

        path = Path(safe_name)
        stem = path.stem or f"attachment-{index}"
        suffix = path.suffix.lower()
        if not suffix and self.mime_type:
            mime_key = normalize_mime_type(self.mime_type) or ""
            suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "application/pdf": ".pdf",
                "text/plain": ".txt",
            }.get(mime_key, "")
        if self.is_audio() and not audio_content_type_for_input(
            mime_type=None,
            file_name=f"attachment{suffix}" if suffix else None,
            source_url=None,
        ):
            suffix = audio_extension_for_input(
                mime_type=self.mime_type,
                file_name=self.file_name,
                source_url=self.source_url,
                default=".ogg",
            )
        normalized_token = token.strip() or "attachment"
        return f"{stem[:64]}-{normalized_token[:8]}{suffix}"


@dataclass(frozen=True)
class SavedDiscordAttachment:
    original_name: str
    path: Path
    mime_type: Optional[str]
    size_bytes: int
    is_audio: bool
    is_image: bool
    transcript_text: Optional[str] = None
    transcript_warning: Optional[str] = None


@dataclass(frozen=True)
class AttachmentContextPayload:
    prompt_text: str
    saved_count: int
    failed_count: int
    user_visible_transcript: Optional[str]
    native_input_items_payload: Optional[list[dict[str, str]]]


@dataclass(frozen=True)
class DiscordMessagePayload:
    content: str
    components: Optional[Sequence[dict[str, Any]]] = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": format_discord_message(self.content),
        }
        if self.components is not None:
            payload["components"] = list(self.components)
        return payload


@dataclass(frozen=True)
class DiscordUpdateNoticeContext:
    chat_id: str

    @classmethod
    def from_raw(
        cls, raw: Mapping[str, object]
    ) -> Optional["DiscordUpdateNoticeContext"]:
        chat_raw = raw.get("chat_id")
        if isinstance(chat_raw, int) and not isinstance(chat_raw, bool):
            return cls(chat_id=str(chat_raw))
        chat_id = _clean_optional_text(chat_raw)
        if chat_id is None:
            return None
        return cls(chat_id=chat_id)


def build_attachment_context_payload(
    *,
    prompt_text: str,
    saved: Sequence[SavedDiscordAttachment],
    failed: int,
    inbox_path: Path,
    outbox_path: Path,
    outbox_pending_path: Path,
    max_message_length: int,
    voice_provider_name: Optional[str],
    whisper_transcript_disclaimer: Optional[str],
) -> AttachmentContextPayload:
    transcript_lines: list[str] = []
    transcript_items = [item for item in saved if item.transcript_text]
    if len(transcript_items) == 1:
        transcript_lines = ["User:", transcript_items[0].transcript_text or ""]
    elif transcript_items:
        transcript_lines = ["User:"]
        for item in transcript_items:
            transcript_lines.append(f"[{item.original_name}]")
            transcript_lines.append(item.transcript_text or "")
            transcript_lines.append("")
        while transcript_lines and not transcript_lines[-1].strip():
            transcript_lines.pop()

    user_visible_transcript = None
    if transcript_lines:
        transcript_text = "\n".join(transcript_lines)
        user_visible_transcript = truncate_for_discord(
            format_discord_message(transcript_text),
            max_len=max(int(max_message_length), 32),
        )

    details: list[str] = ["Inbound Discord attachments:"]
    for item in saved:
        details.append(f"- Name: {item.original_name}")
        details.append(f"  Saved to: {item.path}")
        details.append(f"  Size: {item.size_bytes} bytes")
        if item.mime_type:
            details.append(f"  Mime: {item.mime_type}")
        if item.transcript_text:
            details.append(f"  Transcript: {item.transcript_text}")
        elif item.transcript_warning:
            details.append(f"  Transcript: {item.transcript_warning}")

    if transcript_items and voice_provider_name == "openai_whisper":
        disclaimer = _clean_optional_text(whisper_transcript_disclaimer)
        if disclaimer:
            details.append("")
            details.append(wrap_injected_context(disclaimer))

    if any(not item.is_audio for item in saved):
        details.append("")
        details.append(
            wrap_injected_context(
                "\n".join(
                    [
                        f"Inbox: {inbox_path}",
                        f"Outbox: {outbox_path}",
                        f"Outbox (pending): {outbox_pending_path}",
                        "Use inbox files as local inputs and place reply files in outbox.",
                    ]
                )
            )
        )

    attachment_context = "\n".join(details)
    native_input_items = [
        {"type": "localImage", "path": str(item.path)}
        for item in saved
        if item.is_image
    ]
    native_input_items_payload = native_input_items or None
    if prompt_text.strip():
        separator = "\n" if prompt_text.endswith("\n") else "\n\n"
        combined_prompt = f"{prompt_text}{separator}{attachment_context}"
    else:
        combined_prompt = attachment_context
    return AttachmentContextPayload(
        prompt_text=combined_prompt,
        saved_count=len(saved),
        failed_count=failed,
        user_visible_transcript=user_visible_transcript,
        native_input_items_payload=native_input_items_payload,
    )


def format_discord_approval_prompt(request: Mapping[str, Any]) -> str:
    method = request.get("method")
    params_value = request.get("params")
    params: dict[str, Any] = params_value if isinstance(params_value, dict) else {}
    lines = ["Approval required"]
    reason = params.get("reason")
    if isinstance(reason, str) and reason:
        lines.append(f"Reason: {reason}")
    if method == "item/commandExecution/requestApproval":
        command = params.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command).strip()
        if isinstance(command, str) and command:
            lines.append(f"Command: {command}")
    elif method == "item/fileChange/requestApproval":
        files = params.get("paths")
        if isinstance(files, list):
            normalized = [str(path).strip() for path in files if str(path).strip()]
            if len(normalized) == 1:
                lines.append(f"File: {normalized[0]}")
            elif normalized:
                lines.append("Files:")
                lines.extend(f"- {path}" for path in normalized[:10])
                if len(normalized) > 10:
                    lines.append("- ...")
    return "\n".join(lines)


def build_discord_approval_components(token: str) -> tuple[dict[str, Any], ...]:
    return (
        build_action_row(
            [
                build_button(
                    "Accept",
                    f"approval:{token}:accept",
                    style=DISCORD_BUTTON_STYLE_SUCCESS,
                ),
                build_button("Decline", f"approval:{token}:decline"),
            ]
        ),
        build_action_row([build_button("Cancel", f"approval:{token}:cancel")]),
    )


def build_discord_approval_message(
    request: Mapping[str, Any],
    *,
    token: str,
) -> DiscordMessagePayload:
    return DiscordMessagePayload(
        content=format_discord_approval_prompt(request),
        components=build_discord_approval_components(token),
    )


def build_discord_queue_notice_message(
    *,
    source_message_id: Optional[str],
    content: Optional[str] = None,
    allow_interrupt: bool = True,
) -> DiscordMessagePayload:
    components: Optional[Sequence[dict[str, Any]]] = None
    if source_message_id:
        components = (
            build_queue_notice_buttons(
                source_message_id,
                allow_interrupt=allow_interrupt,
            ),
        )
    return DiscordMessagePayload(
        content=content or "Queued (waiting for available worker...)",
        components=components,
    )


def build_discord_queue_status_message(
    *,
    queued_items: Sequence[tuple[int, str]],
    content: str,
    allow_interrupt: bool = True,
) -> DiscordMessagePayload:
    components = (
        tuple(
            build_queue_status_buttons(
                queued_items,
                allow_interrupt=allow_interrupt,
            )
        )
        or None
    )
    return DiscordMessagePayload(content=content, components=components)


def format_discord_update_status_message(status: Optional[dict[str, Any]]) -> str:
    rendered = format_update_status_message(status)
    if not status:
        return rendered
    lines = [rendered]
    repo_ref = _clean_optional_text(status.get("repo_ref"))
    if repo_ref:
        lines.append(f"Ref: {repo_ref}")
    log_path = _clean_optional_text(status.get("log_path"))
    if log_path:
        lines.append(f"Log: {log_path}")
    return "\n".join(lines)


def format_hub_flow_overview_line(
    *,
    line_label: str,
    is_worktree: bool,
    status: Optional[str],
    done_count: int,
    total_count: int,
    run_id: Optional[str],
    duration_label: Optional[str],
    freshness: Optional[Mapping[str, Any]],
) -> str:
    display = build_ticket_flow_display(
        status=status,
        done_count=done_count,
        total_count=total_count,
        run_id=run_id,
    )
    run_suffix = f" run {run_id}" if run_id else ""
    duration_suffix = f" · took {duration_label}" if duration_label else ""
    freshness_suffix = ""
    freshness_summary = summarize_flow_freshness(freshness)
    if isinstance(freshness, Mapping) and freshness.get("is_stale") is True:
        freshness_suffix = (
            f" · snapshot {freshness_summary}"
            if freshness_summary
            else " · snapshot stale"
        )
    line_prefix = "  -> " if is_worktree else ""
    return (
        f"{line_prefix}{display['status_icon']} {line_label}: "
        f"{display['status_label']} {display['done_count']}/{display['total_count']}"
        f"{run_suffix}{duration_suffix}{freshness_suffix}"
    )


__all__ = [
    "AttachmentContextPayload",
    "DiscordAttachmentAdapter",
    "DiscordMessagePayload",
    "DiscordUpdateNoticeContext",
    "SavedDiscordAttachment",
    "build_attachment_context_payload",
    "build_discord_approval_components",
    "build_discord_approval_message",
    "build_discord_queue_notice_message",
    "build_discord_queue_status_message",
    "format_discord_approval_prompt",
    "format_discord_update_status_message",
    "format_hub_flow_overview_line",
]
