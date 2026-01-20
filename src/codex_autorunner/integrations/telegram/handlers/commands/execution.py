from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .....core.injected_context import wrap_injected_context
from .....core.logging_utils import log_event
from ...adapter import (
    TelegramMessage,
)
from ...constants import WHISPER_TRANSCRIPT_DISCLAIMER

PROMPT_CONTEXT_RE = re.compile(r"\bprompt\b", re.IGNORECASE)
PROMPT_CONTEXT_HINT = (
    "If the user asks to write a prompt, put the prompt in a ```code block```."
)
OUTBOX_CONTEXT_RE = re.compile(
    r"(?:\b(?:pdf|png|jpg|jpeg|gif|webp|svg|csv|tsv|json|yaml|yml|zip|tar|"
    r"gz|tgz|xlsx|xls|docx|pptx|md|txt|log|html|xml)\b|"
    r"\.(?:pdf|png|jpg|jpeg|gif|webp|svg|csv|tsv|json|yaml|yml|zip|tar|"
    r"gz|tgz|xlsx|xls|docx|pptx|md|txt|log|html|xml)\b|"
    r"\b(?:outbox)\b)",
    re.IGNORECASE,
)


@dataclass
class _TurnRunResult:
    record: "TelegramTopicRecord"
    thread_id: Optional[str]
    turn_id: Optional[str]
    response: str
    placeholder_id: Optional[int]
    elapsed_seconds: Optional[float]
    token_usage: Optional[dict[str, Any]]
    transcript_message_id: Optional[int]
    transcript_text: Optional[str]


@dataclass
class _TurnRunFailure:
    failure_message: str
    placeholder_id: Optional[int]
    transcript_message_id: Optional[int]
    transcript_text: Optional[str]


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


class ExecutionCommands:
    """Execution-related command handlers for Telegram integration."""

    def _maybe_append_whisper_disclaimer(
        self, prompt: str, transcript_text: Optional[str]
    ) -> str:
        """Append whisper disclaimer if transcript was provided."""
        if not transcript_text:
            return prompt
        if "voice" in prompt.lower() or "transcript" in prompt.lower():
            return prompt
        return f"{prompt}\n\n{wrap_injected_context(WHISPER_TRANSCRIPT_DISCLAIMER)}"

    async def _maybe_inject_github_context(
        self, message: TelegramMessage, prompt: str
    ) -> str:
        """Inject GitHub context if GitHub URLs are found in the message."""
        links = find_github_links(message.text or "")
        if not links:
            return prompt
        context_lines = []
        for link in links:
            try:
                context_lines.append(f"GitHub: {link}")
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.github_context.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    url=link,
                    exc=exc,
                )
        if not context_lines:
            return prompt
        return f"{prompt}\n\n" + "\n".join(context_lines)

    def _maybe_inject_prompt_context(self, prompt: str, text: str) -> str:
        """Inject prompt context if user asks for a prompt."""
        if not PROMPT_CONTEXT_RE.search(text):
            return prompt
        return f"{prompt}\n\n{wrap_injected_context(PROMPT_CONTEXT_HINT)}"

    def _maybe_inject_car_context(self, prompt: str, text: str) -> str:
        """Inject CAR context if user asks about CAR documents."""
        keywords = ("car", "codex", "todo", "progress", "opinions", "spec", "summary")
        if not any(kw in text.lower() for kw in keywords):
            return prompt
        hint = "Context: read .codex-autorunner/ABOUT_CAR.md for repo-specific rules."
        return f"{prompt}\n\n{hint}"

    def _maybe_inject_outbox_context(
        self, prompt: str, text: str, inbox_path: Path, outbox_pending_path: Path
    ) -> str:
        """Inject outbox context if user mentions file types or outbox."""
        if not OUTBOX_CONTEXT_RE.search(text):
            return prompt
        inbox_count = len(list(inbox_path.glob("*"))) if inbox_path.exists() else 0
        outbox_count = (
            len(list(outbox_pending_path.glob("*")))
            if outbox_pending_path.exists()
            else 0
        )
        hint = (
            f"Files in inbox: {inbox_count}\n"
            f"Files in outbox: {outbox_count}\n"
            f"Use /files to manage inbox and outbox."
        )
        return f"{prompt}\n\n{hint}"

    async def _handle_bang_shell(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        """Handle !shell command."""
        raise NotImplementedError("_handle_bang_shell not yet extracted")

    async def _handle_diff(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        """Handle /diff command."""
        raise NotImplementedError("_handle_diff not yet extracted")

    async def _handle_mention(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        """Handle @mention command."""
        raise NotImplementedError("_handle_mention not yet extracted")


if TYPE_CHECKING:
    from ...helpers import find_github_links
    from ...state import TelegramTopicRecord
