from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .....agents.opencode.runtime import extract_session_id
from .....core.git_utils import GitError
from .....core.logging_utils import log_event
from .....core.pma_context import clear_pma_prompt_state_sessions
from .....core.state import now_iso
from .....core.utils import canonicalize_path
from ....app_server.client import CodexAppServerError
from ....chat.constants import APP_SERVER_UNAVAILABLE_MESSAGE, TOPIC_NOT_BOUND_MESSAGE
from ....chat.newt_support import (
    NewtSetupCommandsError,
    build_newt_reject_lines,
    build_telegram_newt_branch_name,
    describe_newt_reject_state,
    format_newt_submodule_summary,
    is_newt_dirty_worktree_error,
    run_newt_branch_reset,
)
from ....chat.session_messages import (
    build_branch_reset_started_lines,
    build_fresh_session_started_lines,
    build_reset_state_lines,
    build_thread_detail_lines,
)
from ...adapter import TelegramMessage
from ...config import AppServerUnavailableError
from ...constants import MAX_TOPIC_THREAD_HISTORY
from ...helpers import (
    _clear_thread_mirror,
    _extract_thread_id,
    _set_thread_summary,
)

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord


class WorkspaceSessionCommandsMixin:
    async def _handle_reset(self, message: TelegramMessage) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        pma_enabled = bool(record and record.pma_enabled)
        if pma_enabled:
            registry = getattr(self, "_hub_thread_registry", None)
            if registry and hasattr(self, "_pma_registry_key"):
                pma_key = self._pma_registry_key(record, message)
                if pma_key:
                    registry.reset_thread(pma_key)
                    hub_root = getattr(self, "_hub_root", None)
                    if hub_root is not None:
                        try:
                            clear_pma_prompt_state_sessions(
                                Path(hub_root), keys=(pma_key,)
                            )
                        except OSError as exc:
                            log_event(
                                self._logger,
                                logging.WARNING,
                                "telegram.pma.prompt_state.clear_failed",
                                topic_key=key,
                                chat_id=message.chat_id,
                                thread_id=message.thread_id,
                                pma_key=pma_key,
                                exc=exc,
                            )
            if getattr(self._config, "root", None) is not None and callable(
                getattr(self, "_spawn_task", None)
            ):
                from .execution import _reset_telegram_thread_binding

                hub_root = getattr(self, "_hub_root", None)
                if hub_root is not None and record is not None:
                    try:
                        await _reset_telegram_thread_binding(
                            self,
                            surface_key=key,
                            workspace_root=canonicalize_path(Path(hub_root)),
                            agent=self._effective_runtime_agent(record),
                            agent_profile=self._effective_agent_profile(record),
                            repo_id=(
                                record.repo_id.strip()
                                if isinstance(record.repo_id, str)
                                and record.repo_id.strip()
                                else None
                            ),
                            resource_kind=(
                                record.resource_kind.strip()
                                if isinstance(record.resource_kind, str)
                                and record.resource_kind.strip()
                                else None
                            ),
                            resource_id=(
                                record.resource_id.strip()
                                if isinstance(record.resource_id, str)
                                and record.resource_id.strip()
                                else None
                            ),
                            mode="pma",
                            pma_enabled=True,
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.pma.reset.managed_thread_reset_failed",
                            topic_key=key,
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            exc=exc,
                        )
                        await self._send_message(
                            message.chat_id,
                            "Failed to reset PMA thread; check logs for details.",
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                        return
                    update_topic = getattr(self._router, "update_topic", None)
                    if callable(update_topic):
                        await update_topic(
                            message.chat_id,
                            message.thread_id,
                            _clear_thread_mirror,
                        )
            await self._send_message(
                message.chat_id,
                "\n".join(
                    build_reset_state_lines(
                        mode_label="PMA",
                        actor_label=self._effective_agent_label(record),
                        state_label="fresh state",
                    )
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        agent = self._effective_agent(record)
        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                await self._send_message(
                    message.chat_id,
                    "Workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                client = await supervisor.get_client(workspace_root)
                session = await client.create_session(directory=str(workspace_root))
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.opencode.session.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to reset OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to reset OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

            def apply(record: "TelegramTopicRecord") -> None:
                record.active_thread_id = session_id
                if session_id in record.thread_ids:
                    record.thread_ids.remove(session_id)
                record.thread_ids.insert(0, session_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    session_id,
                    last_used_at=now_iso(),
                    workspace_path=record.workspace_path,
                    rollout_path=record.rollout_path,
                )

            await self._router.update_topic(message.chat_id, message.thread_id, apply)
            thread_id = session_id
        else:
            try:
                client = await self._client_for_workspace(record.workspace_path)
            except AppServerUnavailableError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.unavailable",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    APP_SERVER_UNAVAILABLE_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    TOPIC_NOT_BOUND_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                thread = await client.thread_start(
                    record.workspace_path,
                    **self._thread_start_kwargs(record),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.reset.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    workspace_path=record.workspace_path,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to reset thread; check logs for details.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if not await self._require_thread_workspace(
                message, record.workspace_path, thread, action="thread_start"
            ):
                return
            thread_id = _extract_thread_id(thread)
            if not thread_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to reset thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._apply_thread_result(
                message.chat_id, message.thread_id, thread, active_thread_id=thread_id
            )
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    *build_reset_state_lines(
                        mode_label="repo",
                        actor_label=self._effective_agent_label(record),
                        state_label="fresh state",
                    ),
                    *build_thread_detail_lines(
                        headline=f"Started new thread `{thread_id}`.",
                        actor_label=self._effective_agent_label(record),
                        effort=effort_label,
                    ),
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_new(self, message: TelegramMessage) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        pma_enabled = bool(record and record.pma_enabled)
        if pma_enabled:
            registry = getattr(self, "_hub_thread_registry", None)
            if registry and hasattr(self, "_pma_registry_key"):
                pma_key = self._pma_registry_key(record, message)
                if pma_key:
                    registry.reset_thread(pma_key)
                    hub_root = getattr(self, "_hub_root", None)
                    if hub_root is not None:
                        try:
                            clear_pma_prompt_state_sessions(
                                Path(hub_root), keys=(pma_key,)
                            )
                        except OSError as exc:
                            log_event(
                                self._logger,
                                logging.WARNING,
                                "telegram.pma.prompt_state.clear_failed",
                                topic_key=key,
                                chat_id=message.chat_id,
                                thread_id=message.thread_id,
                                pma_key=pma_key,
                                exc=exc,
                            )
            if getattr(self._config, "root", None) is not None and callable(
                getattr(self, "_spawn_task", None)
            ):
                from .execution import _reset_telegram_thread_binding

                hub_root = getattr(self, "_hub_root", None)
                if hub_root is not None and record is not None:
                    try:
                        await _reset_telegram_thread_binding(
                            self,
                            surface_key=key,
                            workspace_root=canonicalize_path(Path(hub_root)),
                            agent=self._effective_runtime_agent(record),
                            agent_profile=self._effective_agent_profile(record),
                            repo_id=(
                                record.repo_id.strip()
                                if isinstance(record.repo_id, str)
                                and record.repo_id.strip()
                                else None
                            ),
                            resource_kind=(
                                record.resource_kind.strip()
                                if isinstance(record.resource_kind, str)
                                and record.resource_kind.strip()
                                else None
                            ),
                            resource_id=(
                                record.resource_id.strip()
                                if isinstance(record.resource_id, str)
                                and record.resource_id.strip()
                                else None
                            ),
                            mode="pma",
                            pma_enabled=True,
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.pma.new.managed_thread_reset_failed",
                            topic_key=key,
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            exc=exc,
                        )
                        await self._send_message(
                            message.chat_id,
                            "Failed to reset PMA session; check logs for details.",
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                        return
                    update_topic = getattr(self._router, "update_topic", None)
                    if callable(update_topic):
                        await update_topic(
                            message.chat_id,
                            message.thread_id,
                            _clear_thread_mirror,
                        )
            await self._send_message(
                message.chat_id,
                "\n".join(
                    build_fresh_session_started_lines(
                        mode_label="PMA",
                        actor_label=self._effective_agent_label(record),
                        state_label="new thread ready",
                    )
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        agent = self._effective_agent(record)
        from .execution import _sync_telegram_thread_binding

        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                await self._send_message(
                    message.chat_id,
                    "Workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                client = await supervisor.get_client(workspace_root)
                session = await client.create_session(directory=str(workspace_root))
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.opencode.session.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

            def apply(record: "TelegramTopicRecord") -> None:
                record.active_thread_id = session_id
                if session_id in record.thread_ids:
                    record.thread_ids.remove(session_id)
                record.thread_ids.insert(0, session_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    session_id,
                    last_used_at=now_iso(),
                    workspace_path=record.workspace_path,
                    rollout_path=record.rollout_path,
                )

            await self._router.update_topic(message.chat_id, message.thread_id, apply)
            thread_id = session_id
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=workspace_root,
                agent=self._effective_runtime_agent(record),
                repo_id=(
                    record.repo_id.strip()
                    if isinstance(record.repo_id, str) and record.repo_id.strip()
                    else None
                ),
                resource_kind=(
                    record.resource_kind.strip()
                    if isinstance(record.resource_kind, str)
                    and record.resource_kind.strip()
                    else None
                ),
                resource_id=(
                    record.resource_id.strip()
                    if isinstance(record.resource_id, str)
                    and record.resource_id.strip()
                    else None
                ),
                backend_thread_id=thread_id,
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )
        else:
            try:
                client = await self._client_for_workspace(record.workspace_path)
            except AppServerUnavailableError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.unavailable",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    APP_SERVER_UNAVAILABLE_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    TOPIC_NOT_BOUND_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                thread = await client.thread_start(
                    record.workspace_path,
                    **self._thread_start_kwargs(record),
                )
            except (OSError, RuntimeError, ValueError, CodexAppServerError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.new.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    workspace_path=record.workspace_path,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new thread; check logs for details.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if not await self._require_thread_workspace(
                message, record.workspace_path, thread, action="thread_start"
            ):
                return
            thread_id = _extract_thread_id(thread)
            if not thread_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._apply_thread_result(
                message.chat_id, message.thread_id, thread, active_thread_id=thread_id
            )
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=Path(record.workspace_path),
                agent=self._effective_runtime_agent(record),
                repo_id=(
                    record.repo_id.strip()
                    if isinstance(record.repo_id, str) and record.repo_id.strip()
                    else None
                ),
                resource_kind=(
                    record.resource_kind.strip()
                    if isinstance(record.resource_kind, str)
                    and record.resource_kind.strip()
                    else None
                ),
                resource_id=(
                    record.resource_id.strip()
                    if isinstance(record.resource_id, str)
                    and record.resource_id.strip()
                    else None
                ),
                backend_thread_id=thread_id,
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    *build_fresh_session_started_lines(
                        mode_label="repo",
                        actor_label=self._effective_agent_label(record),
                        state_label="new thread ready",
                    ),
                    *build_thread_detail_lines(
                        headline=f"Started new thread `{thread_id}`.",
                        workspace_path=record.workspace_path,
                        actor_label=self._effective_agent_label(record),
                        model=record.model or "default",
                        effort=effort_label,
                    ),
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_newt(self, message: TelegramMessage) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        pma_enabled = bool(record and record.pma_enabled)
        if pma_enabled:
            await self._send_message(
                message.chat_id,
                "/newt is not available in PMA mode. Use /new instead.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await self._send_message(
                message.chat_id,
                "Workspace unavailable.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        branch_name = build_telegram_newt_branch_name(
            chat_id=message.chat_id,
            workspace_root=workspace_root,
            thread_id=message.thread_id,
            message_id=message.message_id,
            update_id=message.update_id,
        )
        had_previous = bool(record.active_thread_id)

        try:
            reset_result = await run_newt_branch_reset(
                workspace_root,
                branch_name,
                repo_id_hint=(
                    record.repo_id.strip()
                    if isinstance(record.repo_id, str) and record.repo_id.strip()
                    else None
                ),
                hub_client=getattr(self, "_hub_client", None),
            )
        except NewtSetupCommandsError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.newt.setup.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                workspace_path=str(workspace_root),
                exc=exc,
            )
            detail = format_newt_submodule_summary("/newt", exc.submodule_paths)
            suffix = f"\n\n{detail}" if detail else ""
            await self._send_message(
                message.chat_id,
                (
                    f"Reset branch `{branch_name}` to `origin/{exc.default_branch}` "
                    f"but setup commands failed: {exc}{suffix}"
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        except GitError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.newt.branch_reset.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                branch=branch_name,
                exc=exc,
            )
            if is_newt_dirty_worktree_error(exc):
                reject_state = describe_newt_reject_state(workspace_root)
                reasons = list(reject_state.reasons)
                if not reasons:
                    reasons = ["Local git changes are blocking the reset."]
                await self._send_message(
                    message.chat_id,
                    "\n".join(
                        build_newt_reject_lines(
                            command_label="/newt",
                            reasons=reasons,
                            submodule_paths=reject_state.submodule_paths,
                            resolution_hint=(
                                "Commit or stash local changes, then retry `/newt`."
                            ),
                        )
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                f"Failed to reset branch `{branch_name}` from origin default branch: {exc}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        def apply(record: "TelegramTopicRecord") -> None:
            record.active_thread_id = None
            record.thread_ids = []
            record.thread_summaries = {}
            record.rollout_path = None
            record.pending_compact_seed = None
            record.pending_compact_seed_thread_id = None

        await self._router.update_topic(message.chat_id, message.thread_id, apply)

        agent = self._effective_agent(record)
        from .execution import _sync_telegram_thread_binding

        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; cannot start session.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                client = await supervisor.get_client(workspace_root)
                session = await client.create_session(directory=str(workspace_root))
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.newt.opencode.session.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Reset branch but failed to start OpenCode session.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Reset branch but failed to start OpenCode session.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

            def apply_session(record: "TelegramTopicRecord") -> None:
                record.active_thread_id = session_id
                if session_id in record.thread_ids:
                    record.thread_ids.remove(session_id)
                record.thread_ids.insert(0, session_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]

            await self._router.update_topic(
                message.chat_id, message.thread_id, apply_session
            )
            thread_id = session_id
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=workspace_root,
                agent=self._effective_runtime_agent(record),
                repo_id=(
                    record.repo_id.strip()
                    if isinstance(record.repo_id, str) and record.repo_id.strip()
                    else None
                ),
                resource_kind=(
                    record.resource_kind.strip()
                    if isinstance(record.resource_kind, str)
                    and record.resource_kind.strip()
                    else None
                ),
                resource_id=(
                    record.resource_id.strip()
                    if isinstance(record.resource_id, str)
                    and record.resource_id.strip()
                    else None
                ),
                backend_thread_id=thread_id,
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )
        else:
            try:
                client = await self._client_for_workspace(str(workspace_root))
            except AppServerUnavailableError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.unavailable",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Reset branch but app server unavailable. Try sending a message.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    "Reset branch but workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                thread = await client.thread_start(
                    str(workspace_root),
                    **self._thread_start_kwargs(record),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.newt.thread_start.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Reset branch but failed to start thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            thread_id = _extract_thread_id(thread)
            if not thread_id:
                await self._send_message(
                    message.chat_id,
                    "Reset branch but failed to start thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._apply_thread_result(
                message.chat_id, message.thread_id, thread, active_thread_id=thread_id
            )
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=workspace_root,
                agent=self._effective_runtime_agent(record),
                repo_id=(
                    record.repo_id.strip()
                    if isinstance(record.repo_id, str) and record.repo_id.strip()
                    else None
                ),
                resource_kind=(
                    record.resource_kind.strip()
                    if isinstance(record.resource_kind, str)
                    and record.resource_kind.strip()
                    else None
                ),
                resource_id=(
                    record.resource_id.strip()
                    if isinstance(record.resource_id, str)
                    and record.resource_id.strip()
                    else None
                ),
                backend_thread_id=thread_id,
                mode="repo",
                pma_enabled=False,
                replace_existing=True,
            )

        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        submodule_summary = format_newt_submodule_summary(
            "/newt", reset_result.submodule_paths
        )
        message_lines = [
            *build_branch_reset_started_lines(
                branch_name=branch_name,
                default_branch=reset_result.default_branch,
                mode_label="repo",
                actor_label=self._effective_agent_label(record),
                state_label=(
                    "cleared previous thread" if had_previous else "new thread ready"
                ),
                setup_command_count=reset_result.setup_command_count or None,
            ),
            *([submodule_summary] if submodule_summary else []),
            *build_thread_detail_lines(
                headline=f"Started new thread `{thread_id}`.",
                workspace_path=str(workspace_root),
                actor_label=self._effective_agent_label(record),
                model=record.model or "default",
                effort=effort_label,
            ),
        ]
        await self._send_message(
            message.chat_id,
            "\n".join(message_lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_archive(self, message: TelegramMessage) -> None:
        from .....core.archive import (
            archive_workspace_for_fresh_start,
            resolve_workspace_archive_target,
        )

        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        if bool(record and record.pma_enabled):
            await self._send_message(
                message.chat_id,
                "/archive is not available in PMA mode. Use /new instead.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await self._send_message(
                message.chat_id,
                "Workspace unavailable.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        manifest_path = (
            self._hub_root / ".codex-autorunner" / "manifest.yml"
            if self._hub_root is not None
            else None
        )
        try:
            target = resolve_workspace_archive_target(
                workspace_root,
                hub_root=self._hub_root,
                manifest_path=manifest_path,
            )
            result = await asyncio.to_thread(
                archive_workspace_for_fresh_start,
                hub_root=self._hub_root,
                base_repo_root=target.base_repo_root,
                base_repo_id=target.base_repo_id,
                worktree_repo_root=workspace_root,
                worktree_repo_id=target.workspace_repo_id,
                branch=None,
                worktree_of=target.worktree_of,
                note="Telegram /archive",
                source_path=target.source_path,
            )
        except ValueError as exc:
            await self._send_message(
                message.chat_id,
                str(exc),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.archive_state.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                workspace_path=record.workspace_path,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Archive failed; check logs for details.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        if getattr(self, "_hub_root", None) is not None and record is not None:
            from .execution import _reset_telegram_thread_binding

            try:
                await _reset_telegram_thread_binding(
                    self,
                    surface_key=key,
                    workspace_root=workspace_root,
                    agent=self._effective_runtime_agent(record),
                    agent_profile=self._effective_agent_profile(record),
                    repo_id=(
                        record.repo_id.strip()
                        if isinstance(record.repo_id, str) and record.repo_id.strip()
                        else None
                    ),
                    resource_kind=(
                        record.resource_kind.strip()
                        if isinstance(record.resource_kind, str)
                        and record.resource_kind.strip()
                        else None
                    ),
                    resource_id=(
                        record.resource_id.strip()
                        if isinstance(record.resource_id, str)
                        and record.resource_id.strip()
                        else None
                    ),
                    mode="repo",
                    pma_enabled=False,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.archive.managed_thread_reset_failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    workspace_path=str(workspace_root),
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Archive completed, but preparing a fresh managed thread failed.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            _clear_thread_mirror,
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    (
                        f"Archived workspace state to snapshot `{result.snapshot_id}`."
                        if result.snapshot_id
                        else "Workspace CAR state was already clean."
                    ),
                    f"Archived paths: {', '.join(result.archived_paths) or 'none'}",
                    (
                        f"Archived {len(result.archived_thread_ids)} managed thread"
                        f"{'' if len(result.archived_thread_ids) == 1 else 's'}."
                    ),
                    "The binding remains active for fresh work.",
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
