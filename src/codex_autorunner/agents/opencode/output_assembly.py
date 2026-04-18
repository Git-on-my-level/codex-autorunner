"""OpenCode output assembler: message-role tracking, text dedupe, usage snapshots, and final-output recovery.

Owns the bookkeeping that turns a stream of SSE events into a single
:class:`OpenCodeTurnOutput`.  The assembler accepts normalized event data and
explicit recovery inputs; it does *not* own stream lifecycle policy (timeouts,
reconnect, backoff).  That separation lets the assembler be tested without
exercising reconnect/stall logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx

from ...core.coercion import coerce_int
from ...core.logging_utils import log_event
from .constants import OPENCODE_MODEL_CONTEXT_KEYS
from .event_fields import (
    extract_message_id as extract_event_message_id,
)
from .event_fields import (
    extract_message_role as extract_event_message_role,
)
from .protocol_payload import (
    extract_context_window,
    extract_message_phase,
    extract_model_ids,
    extract_total_tokens,
    extract_usage_details,
    parse_message_response,
    prompt_echo_matches,
    recover_last_assistant_message,
)
from .usage_decoder import extract_usage

PartHandler = Callable[[str, dict[str, Any], Optional[str]], Awaitable[None]]


@dataclass(frozen=True)
class OutputAssemblyResult:
    text: str
    error: Optional[str] = None
    usage: Optional[dict[str, Any]] = None


class OutputAssembler:
    """Stateful assembler that tracks output for one OpenCode turn.

    Feed events via the ``on_*`` methods during the stream loop, then call
    :meth:`build_result` after the loop ends to get the final
    :class:`OutputAssemblyResult`.

    Callers should call :meth:`flush_pending` when the stream ends before
    calling :meth:`build_result`, or rely on :meth:`build_result` to handle
    already-flushed text.
    """

    def __init__(
        self,
        *,
        session_id: str,
        prompt: Optional[str] = None,
        model_payload: Optional[dict[str, str]] = None,
        session_fetcher: Optional[Callable[[], Awaitable[Any]]] = None,
        provider_fetcher: Optional[Callable[[], Awaitable[Any]]] = None,
        messages_fetcher: Optional[Callable[[], Awaitable[Any]]] = None,
        part_handler: Optional[PartHandler] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._session_id = session_id
        self._prompt = prompt
        self._model_payload = model_payload
        self._session_fetcher = session_fetcher
        self._provider_fetcher = provider_fetcher
        self._messages_fetcher = messages_fetcher
        self._part_handler = part_handler
        self._logger = logger or logging.getLogger(__name__)

        self._text_parts: list[str] = []
        self._part_lengths: dict[str, int] = {}
        self._last_full_text = ""
        self._error: Optional[str] = None
        self._message_roles: dict[str, str] = {}
        self._message_roles_seen = False
        self._pending_text: dict[str, list[str]] = {}
        self._pending_no_id: list[str] = []
        self._no_id_role: Optional[str] = None
        self._fallback_message: Optional[tuple[Optional[str], Optional[str], str]] = (
            None
        )
        self._last_completed_assistant_text: Optional[str] = None
        self._last_usage_signature: Optional[
            tuple[
                Optional[str],
                Optional[str],
                Optional[int],
                Optional[int],
                Optional[int],
                Optional[int],
                Optional[int],
                Optional[int],
            ]
        ] = None
        self._latest_usage_snapshot: Optional[dict[str, Any]] = None
        self._part_types: dict[str, str] = {}
        self._providers_cache: Optional[list[dict[str, Any]]] = None
        self._context_window_cache: dict[str, Optional[int]] = {}
        self._session_model_ids: Optional[tuple[Optional[str], Optional[str]]] = None
        self._default_model_ids = (
            extract_model_ids(model_payload)
            if isinstance(model_payload, dict)
            else None
        )
        self._prompt_response_text: Optional[str] = None
        self._prompt_response_error: Optional[str] = None

    @property
    def has_pending_text(self) -> bool:
        return bool(self._pending_text or self._pending_no_id)

    @property
    def has_text(self) -> bool:
        return bool(self._text_parts)

    @property
    def error(self) -> Optional[str]:
        return self._error

    @error.setter
    def error(self, value: Optional[str]) -> None:
        self._error = value

    def on_register_message_role(
        self, payload: Any
    ) -> tuple[Optional[str], Optional[str]]:
        if not isinstance(payload, dict):
            return None, None
        role = extract_event_message_role(payload)
        msg_id = extract_event_message_id(payload)
        if isinstance(role, str) and msg_id:
            self._message_roles[msg_id] = role
            self._message_roles_seen = True
        return msg_id, role

    def on_handle_role_update(
        self, message_id: Optional[str], role: Optional[str]
    ) -> None:
        if not role:
            return
        if role == "assistant":
            self._flush_pending_text(message_id)
            self._flush_pending_no_id_as_assistant()
            return
        if role == "user":
            self._flush_pending_text(message_id)
            self._discard_pending_no_id()
            self._no_id_role = None

    def on_message_completed(
        self,
        payload: Any,
        *,
        is_primary_session: bool,
        event_session_id: Optional[str],
    ) -> Optional[str]:
        """Process a ``message.completed`` / ``message.updated`` event.

        Returns the resolved message role for primary sessions, or ``None``
        for non-primary sessions.
        """
        message_result = parse_message_response(payload)
        msg_id: Optional[str] = None
        role: Optional[str] = None
        if not is_primary_session:
            return None
        msg_id, role = self.on_register_message_role(payload)
        resolved_role = role
        if resolved_role is None and msg_id:
            resolved_role = self._message_roles.get(msg_id)
        if message_result.text:
            if resolved_role == "assistant" or resolved_role is None:
                self._fallback_message = (
                    msg_id,
                    resolved_role,
                    message_result.text,
                )
                if resolved_role is None:
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "opencode.message.completed.role_missing",
                        session_id=event_session_id,
                        message_id=msg_id,
                    )
            else:
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "opencode.message.completed.ignored",
                    session_id=event_session_id,
                    message_id=msg_id,
                    role=resolved_role,
                )
        if message_result.error and not self._error:
            self._error = message_result.error
        return resolved_role

    def on_primary_assistant_completion(
        self, payload: Any, message_role: Optional[str]
    ) -> None:
        """Record primary-session assistant completion for final-output selection.

        Also checks whether the completion phase is ``commentary`` — commentary
        completions do not override ``last_completed_assistant_text``.
        """
        if message_role != "assistant":
            return
        if extract_message_phase(payload) == "commentary":
            return
        message_result = parse_message_response(payload)
        self._last_completed_assistant_text = (
            message_result.text if message_result.text else None
        )

    async def on_text_delta(
        self,
        *,
        part_message_id: Optional[str],
        delta_text: str,
        part_id: Optional[str],
        part_dict: Optional[dict[str, Any]],
    ) -> None:
        """Append a text delta to the output, with dedupe bookkeeping."""
        self._append_text_for_message(part_message_id, delta_text)
        if isinstance(part_id, str) and part_id:
            if isinstance(part_dict, dict):
                text = part_dict.get("text")
                if isinstance(text, str):
                    self._part_lengths[part_id] = len(text)
                else:
                    self._part_lengths[part_id] = self._part_lengths.get(
                        part_id, 0
                    ) + len(delta_text)
            else:
                self._part_lengths[part_id] = self._part_lengths.get(part_id, 0) + len(
                    delta_text
                )
        elif isinstance(part_dict, dict):
            text = part_dict.get("text")
            if isinstance(text, str):
                self._last_full_text = text

    async def on_full_text_part(
        self,
        *,
        part_message_id: Optional[str],
        part_dict: dict[str, Any],
    ) -> None:
        """Append incremental text from a full-text part update (no delta)."""
        text = part_dict.get("text")
        if not isinstance(text, str) or not text:
            return
        part_id = part_dict.get("id") or part_dict.get("partId")
        if isinstance(part_id, str) and part_id:
            last_len = self._part_lengths.get(part_id, 0)
            if len(text) > last_len:
                self._append_text_for_message(part_message_id, text[last_len:])
                self._part_lengths[part_id] = len(text)
        else:
            if self._last_full_text and text.startswith(self._last_full_text):
                self._append_text_for_message(
                    part_message_id, text[len(self._last_full_text) :]
                )
            elif text != self._last_full_text:
                self._append_text_for_message(part_message_id, text)
            self._last_full_text = text

    def flush_pending(self) -> None:
        """Flush all pending text when no final text has been assembled yet."""
        if not self._text_parts and self.has_pending_text:
            self._flush_all_pending_text()

    async def emit_usage_update(
        self, payload: Any, *, is_primary_session: bool
    ) -> None:
        """Extract, deduplicate, and emit usage for the event payload."""
        if not is_primary_session:
            return
        usage = extract_usage(payload)
        if usage is None:
            return
        provider_id, model_id = extract_model_ids(payload)
        if not provider_id or not model_id:
            provider_id, model_id = await self._resolve_session_model_ids()
        total_tokens = extract_total_tokens(usage)
        context_window = extract_context_window(payload, usage)
        if context_window is None:
            context_window = await self._resolve_context_window_from_providers(
                provider_id, model_id
            )
        usage_details = extract_usage_details(usage)
        usage_signature = (
            provider_id,
            model_id,
            total_tokens,
            usage_details.get("inputTokens"),
            usage_details.get("cachedInputTokens"),
            usage_details.get("outputTokens"),
            usage_details.get("reasoningTokens"),
            context_window,
        )
        if usage_signature == self._last_usage_signature:
            return
        self._last_usage_signature = usage_signature
        usage_snapshot: dict[str, Any] = {}
        if provider_id:
            usage_snapshot["providerID"] = provider_id
        if model_id:
            usage_snapshot["modelID"] = model_id
        if total_tokens is not None:
            usage_snapshot["totalTokens"] = total_tokens
        if usage_details:
            usage_snapshot.update(usage_details)
        if context_window is not None:
            usage_snapshot["modelContextWindow"] = context_window
        if usage_snapshot:
            self._latest_usage_snapshot = dict(usage_snapshot)
            if self._part_handler is not None:
                await self._part_handler("usage", usage_snapshot, None)

    def remember_part_type(
        self, part_id: Optional[str], part_type: Optional[str]
    ) -> None:
        if (
            isinstance(part_id, str)
            and part_id
            and isinstance(part_type, str)
            and part_type
        ):
            self._part_types[part_id] = part_type
        elif (
            isinstance(part_id, str)
            and part_id
            and not isinstance(part_type, str)
            and part_id in self._part_types
        ):
            pass
        else:
            return

    def lookup_part_type(self, part_id: Optional[str]) -> Optional[str]:
        if isinstance(part_id, str) and part_id:
            return self._part_types.get(part_id)
        return None

    def on_prompt_response(self, response: Any) -> None:
        if response is None:
            return
        result = parse_message_response(response)
        if result.text:
            self._prompt_response_text = result.text
        if result.error:
            self._prompt_response_error = result.error

    async def build_result(self) -> OutputAssemblyResult:
        """Build the final :class:`OutputAssemblyResult` after the stream loop.

        Applies fallback-message selection and ``messages_fetcher`` recovery
        if no streaming text was collected.
        """
        if not self._text_parts and self._fallback_message is not None:
            msg_id, role, text = self._fallback_message
            resolved_role = role
            if resolved_role is None and msg_id:
                resolved_role = self._message_roles.get(msg_id)
            if resolved_role == "assistant" or (
                resolved_role is None
                and text
                and not prompt_echo_matches(text, prompt=self._prompt)
            ):
                self._text_parts.append(text)

        if not self._text_parts and self._messages_fetcher is not None:
            try:
                messages_payload = await self._messages_fetcher()
            except (
                httpx.HTTPError,
                ValueError,
                OSError,
                AttributeError,
            ) as exc:
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "opencode.messages.fetch_failed",
                    session_id=self._session_id,
                    exc=exc,
                )
            else:
                recovered = recover_last_assistant_message(
                    messages_payload,
                    prompt=self._prompt,
                )
                if recovered.text:
                    self._text_parts.append(recovered.text)
                if recovered.error and not self._error:
                    self._error = recovered.error

        if not self._text_parts and self._prompt_response_text is not None:
            if not prompt_echo_matches(self._prompt_response_text, prompt=self._prompt):
                self._text_parts.append(self._prompt_response_text)
        if not self._error and self._prompt_response_error is not None:
            self._error = self._prompt_response_error

        final_text = self._last_completed_assistant_text or "".join(self._text_parts)
        return OutputAssemblyResult(
            text=final_text.strip(),
            error=self._error,
            usage=self._latest_usage_snapshot,
        )

    def _flush_pending_no_id_as_assistant(self) -> None:
        if self._pending_no_id:
            self._text_parts.extend(self._pending_no_id)
            self._pending_no_id.clear()
        self._no_id_role = "assistant"

    def _discard_pending_no_id(self) -> None:
        if self._pending_no_id:
            self._pending_no_id.clear()

    def _append_text_for_message(self, message_id: Optional[str], text: str) -> None:
        if not text:
            return
        if message_id is None:
            if self._no_id_role == "assistant":
                self._text_parts.append(text)
            else:
                self._pending_no_id.append(text)
            return
        role = self._message_roles.get(message_id)
        if role == "user":
            return
        if role == "assistant":
            self._text_parts.append(text)
            return
        self._pending_text.setdefault(message_id, []).append(text)

    def _flush_pending_text(self, message_id: Optional[str]) -> None:
        if not message_id:
            return
        role = self._message_roles.get(message_id)
        if role != "assistant":
            self._pending_text.pop(message_id, None)
            return
        pending = self._pending_text.pop(message_id, [])
        if pending:
            self._text_parts.extend(pending)

    def _flush_all_pending_text(self) -> None:
        if self._pending_text:
            for pending in list(self._pending_text.values()):
                if pending:
                    self._text_parts.extend(pending)
            self._pending_text.clear()
        if self._pending_no_id:
            if (
                not self._message_roles_seen
                or self._no_id_role == "assistant"
                or not self._text_parts
            ):
                self._text_parts.extend(self._pending_no_id)
            self._pending_no_id.clear()

    async def _resolve_session_model_ids(
        self,
    ) -> tuple[Optional[str], Optional[str]]:
        if self._session_model_ids is not None:
            return self._session_model_ids
        resolved_ids: Optional[tuple[Optional[str], Optional[str]]] = None
        if self._session_fetcher is not None:
            try:
                payload = await self._session_fetcher()
                resolved_ids = extract_model_ids(payload)
            except (httpx.HTTPError, ValueError, OSError):
                resolved_ids = None
        if not resolved_ids or all(value is None for value in resolved_ids):
            resolved_ids = self._default_model_ids
        self._session_model_ids = resolved_ids or (None, None)
        return self._session_model_ids

    async def _resolve_context_window_from_providers(
        self,
        provider_id: Optional[str],
        model_id: Optional[str],
    ) -> Optional[int]:
        if not provider_id or not model_id:
            return None
        cache_key = f"{provider_id}/{model_id}"
        if cache_key in self._context_window_cache:
            return self._context_window_cache[cache_key]
        if self._provider_fetcher is None:
            self._context_window_cache[cache_key] = None
            return None
        if self._providers_cache is None:
            try:
                payload = await self._provider_fetcher()
            except (httpx.HTTPError, ValueError, OSError):
                self._context_window_cache[cache_key] = None
                return None
            providers: list[dict[str, Any]] = []
            if isinstance(payload, dict):
                raw_providers = payload.get("providers")
                if isinstance(raw_providers, list):
                    providers = [
                        entry for entry in raw_providers if isinstance(entry, dict)
                    ]
            elif isinstance(payload, list):
                providers = [entry for entry in payload if isinstance(entry, dict)]
            self._providers_cache = providers
        context_window = None
        for provider in self._providers_cache or []:
            pid = provider.get("id") or provider.get("providerID")
            if pid != provider_id:
                continue
            models = provider.get("models")
            model_entry = None
            if isinstance(models, dict):
                candidate = models.get(model_id)
                if isinstance(candidate, dict):
                    model_entry = candidate
            elif isinstance(models, list):
                for entry in models:
                    if not isinstance(entry, dict):
                        continue
                    entry_id = entry.get("id") or entry.get("modelID")
                    if entry_id == model_id:
                        model_entry = entry
                        break
            if isinstance(model_entry, dict):
                limit = model_entry.get("limit") or model_entry.get("limits")
                if isinstance(limit, dict):
                    for key in OPENCODE_MODEL_CONTEXT_KEYS:
                        value = coerce_int(limit.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
                if context_window is None:
                    for key in OPENCODE_MODEL_CONTEXT_KEYS:
                        value = coerce_int(model_entry.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
            if context_window is None:
                limit = provider.get("limit") or provider.get("limits")
                if isinstance(limit, dict):
                    for key in OPENCODE_MODEL_CONTEXT_KEYS:
                        value = coerce_int(limit.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
            break
        self._context_window_cache[cache_key] = context_window
        return context_window


def apply_prompt_response_fallback(
    output: Any,
    response: Any,
    *,
    prompt: Optional[str] = None,
) -> Any:
    from .runtime import OpenCodeTurnOutput

    if response is None:
        return output
    fallback = parse_message_response(response)
    if not fallback.text and not fallback.error:
        return output
    text = output.text
    error = output.error
    usage = output.usage
    if not text and fallback.text:
        if not prompt_echo_matches(fallback.text, prompt=prompt):
            text = fallback.text
    if not error and fallback.error:
        error = fallback.error
    if text == output.text and error == output.error:
        return output
    return OpenCodeTurnOutput(text=text, error=error, usage=usage)
