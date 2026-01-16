from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from ...core.logging_utils import log_event
from ...core.utils import canonicalize_path
from ...workspace import canonical_workspace_root, workspace_id_for_path
from ..app_server.client import CodexAppServerClient
from ..app_server.env import build_app_server_env
from .constants import (
    APP_SERVER_START_BACKOFF_INITIAL_SECONDS,
    APP_SERVER_START_BACKOFF_MAX_SECONDS,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TurnKey,
)
from .rendering import _format_telegram_html, _format_telegram_markdown
from .state import TOPIC_ROOT, parse_topic_key
from .types import TurnContext


class TelegramRuntimeHelpers:
    def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return self._router.resolve_key(chat_id, thread_id)

    def _canonical_workspace_root(
        self, workspace_path: Optional[str]
    ) -> Optional[Path]:
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            return None
        try:
            return canonical_workspace_root(Path(workspace_path))
        except Exception:
            return None

    def _workspace_id_for_path(self, workspace_path: Optional[str]) -> Optional[str]:
        root = self._canonical_workspace_root(workspace_path)
        if root is None:
            return None
        return workspace_id_for_path(root)

    def _refresh_workspace_id(self, key: str, record) -> Optional[str]:
        if record.workspace_id or not record.workspace_path:
            return record.workspace_id
        workspace_id = self._workspace_id_for_path(record.workspace_path)
        if workspace_id:
            self._store.update_topic(
                key, lambda stored: setattr(stored, "workspace_id", workspace_id)
            )
            record.workspace_id = workspace_id
        return record.workspace_id

    def _build_workspace_env(
        self, workspace_root: Path, workspace_id: str, state_dir: Path
    ) -> dict[str, str]:
        return build_app_server_env(
            self._config.app_server_command,
            workspace_root,
            state_dir,
            logger=self._logger,
            event_prefix="telegram",
        )

    async def _client_for_workspace(
        self, workspace_path: Optional[str]
    ) -> Optional[CodexAppServerClient]:
        workspace_root = self._canonical_workspace_root(workspace_path)
        if workspace_root is None:
            return None
        delay = APP_SERVER_START_BACKOFF_INITIAL_SECONDS
        while True:
            try:
                return await self._app_server_supervisor.get_client(workspace_root)
            except Exception as exc:
                self._log_app_server_start_failure(workspace_root, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, APP_SERVER_START_BACKOFF_MAX_SECONDS)

    def _log_app_server_start_failure(
        self, workspace_root: Path, exc: Exception
    ) -> None:
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.app_server.start_failed",
            workspace_path=str(workspace_root),
            exc=exc,
        )

    def _topic_scope_id(
        self, repo_id: Optional[str], workspace_path: Optional[str]
    ) -> Optional[str]:
        normalized_repo = repo_id.strip() if isinstance(repo_id, str) else ""
        normalized_path = (
            workspace_path.strip() if isinstance(workspace_path, str) else ""
        )
        if normalized_path:
            try:
                normalized_path = str(canonicalize_path(Path(normalized_path)))
            except Exception:
                pass
        if normalized_repo and normalized_path:
            return f"{normalized_repo}@{normalized_path}"
        if normalized_repo:
            return normalized_repo
        if normalized_path:
            return normalized_path
        return None

    def _turn_key(
        self, thread_id: Optional[str], turn_id: Optional[str]
    ) -> Optional[TurnKey]:
        if not isinstance(thread_id, str) or not thread_id:
            return None
        if not isinstance(turn_id, str) or not turn_id:
            return None
        return (thread_id, turn_id)

    def _resolve_turn_key(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[TurnKey]:
        if not isinstance(turn_id, str) or not turn_id:
            return None
        key: Optional[tuple[str, str]] = None
        if thread_id is not None:
            if not isinstance(thread_id, str) or not thread_id:
                return None
            key = (thread_id, turn_id)
            if self._turn_contexts.get(key) is not None:
                return key
        matches = [
            candidate_key
            for candidate_key in self._turn_contexts
            if candidate_key[1] == turn_id
        ]
        if len(matches) == 1:
            candidate = matches[0]
            if key is not None and candidate != key:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.turn.thread_mismatch",
                    turn_id=turn_id,
                    requested_thread_id=thread_id,
                    actual_thread_id=candidate[0],
                )
            return candidate
        if len(matches) > 1:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.turn.ambiguous",
                turn_id=turn_id,
                matches=len(matches),
            )
        return None

    def _resolve_turn_context(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[TurnContext]:
        key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if key is None:
            return None
        return self._turn_contexts.get(key)

    def _register_turn_context(
        self, turn_key: TurnKey, turn_id: str, ctx: TurnContext
    ) -> bool:
        existing = self._turn_contexts.get(turn_key)
        if existing and existing.topic_key != ctx.topic_key:
            log_event(
                self._logger,
                logging.ERROR,
                "telegram.turn.context.collision",
                turn_id=turn_id,
                existing_topic=existing.topic_key,
                new_topic=ctx.topic_key,
            )
            return False
        self._turn_contexts[turn_key] = ctx
        return True

    def _clear_thinking_preview(self, turn_key: TurnKey) -> None:
        self._turn_preview_text.pop(turn_key, None)
        self._turn_preview_updated_at.pop(turn_key, None)
        self._turn_progress_text.pop(turn_key, None)
        self._turn_progress_updated_at.pop(turn_key, None)

    def _build_debug_prefix(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int] = None,
        topic_key: Optional[str] = None,
        workspace_path: Optional[str] = None,
        codex_thread_id: Optional[str] = None,
    ) -> str:
        if not self._config.debug_prefix_context:
            return ""
        resolved_key = topic_key
        if not resolved_key:
            try:
                resolved_key = self._resolve_topic_key(chat_id, thread_id)
            except Exception:
                resolved_key = None
        scope = None
        if resolved_key:
            try:
                _, _, scope = parse_topic_key(resolved_key)
            except Exception:
                scope = None
        record = None
        if workspace_path is None or codex_thread_id is None:
            record = self._router.get_topic(resolved_key) if resolved_key else None
        if workspace_path is None and record is not None:
            workspace_path = record.workspace_path
        if codex_thread_id is None and record is not None:
            codex_thread_id = record.active_thread_id
        parts = [f"chat={chat_id}"]
        thread_label = str(thread_id) if thread_id is not None else TOPIC_ROOT
        parts.append(f"thread={thread_label}")
        if scope:
            parts.append(f"scope={scope}")
        if workspace_path:
            parts.append(f"cwd={workspace_path}")
        if codex_thread_id:
            parts.append(f"codex={codex_thread_id}")
        if reply_to is not None:
            parts.append(f"reply_to={reply_to}")
        return f"[{' '.join(parts)}] "

    def _prepare_outgoing_text(
        self,
        text: str,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int] = None,
        topic_key: Optional[str] = None,
        workspace_path: Optional[str] = None,
        codex_thread_id: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        prefix = self._build_debug_prefix(
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
            topic_key=topic_key,
            workspace_path=workspace_path,
            codex_thread_id=codex_thread_id,
        )
        if prefix:
            text = f"{prefix}{text}"
        return self._prepare_message(text)

    def _render_message(self, text: str) -> tuple[str, Optional[str]]:
        parse_mode = self._config.parse_mode
        if not parse_mode:
            return text, None
        if parse_mode == "HTML":
            return _format_telegram_html(text), parse_mode
        if parse_mode in ("Markdown", "MarkdownV2"):
            return _format_telegram_markdown(text, parse_mode), parse_mode
        return text, parse_mode

    def _prepare_message(self, text: str) -> tuple[str, Optional[str]]:
        rendered, parse_mode = self._render_message(text)
        # Avoid parse_mode when chunking to keep markup intact.
        if parse_mode and len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return rendered, parse_mode
        return text, None
