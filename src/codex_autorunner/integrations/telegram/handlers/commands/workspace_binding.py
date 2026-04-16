from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .....manifest import load_manifest
from ...adapter import TelegramCallbackQuery, TelegramMessage
from ...constants import BIND_PICKER_PROMPT
from ...helpers import _split_topic_key
from ...types import SelectionState

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord


class WorkspaceBindingMixin:
    async def _handle_repos(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        if not self._manifest_path or not self._hub_root:
            await self._send_message(
                message.chat_id,
                "Hub manifest not configured.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        try:
            manifest = load_manifest(self._manifest_path, self._hub_root)
        except (OSError, ValueError) as exc:
            await self._send_message(
                message.chat_id,
                f"Failed to load manifest: {exc}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        lines = ["Repositories:"]
        for repo in manifest.repos:
            if not repo.enabled:
                continue
            lines.append(f"- `{repo.id}` ({repo.path})")

        lines.append("\nUse /bind <repo_id> to switch context.")

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode="Markdown",
        )

    async def _handle_bind(self, message: TelegramMessage, args: str) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        if not args:
            options = self._list_manifest_repos()
            if not options:
                await self._send_message(
                    message.chat_id,
                    "Usage: /bind <repo_id> or /bind <path>.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            items = [(repo_id, repo_id) for repo_id in options]
            state = SelectionState(
                items=items,
                requester_user_id=(
                    str(message.from_user_id)
                    if message.from_user_id is not None
                    else None
                ),
            )
            keyboard = self._build_bind_keyboard(state)
            self._bind_options[key] = state
            self._touch_cache_timestamp("bind_options", key)
            await self._send_message(
                message.chat_id,
                self._selection_prompt(BIND_PICKER_PROMPT, state),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        await self._bind_topic_with_arg(key, args, message)

    async def _selection_bind_topic_by_repo_id(
        self,
        key: str,
        repo_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(repo_id)
        if resolved is None:
            await self._answer_callback(callback, "Repo not found")
            await self._finalize_selection(key, callback, "Repo not found.")
            return
        workspace_path, resolved_repo_id, resource_kind, resource_id = resolved
        chat_id, thread_id = _split_topic_key(key)
        scope = self._topic_scope_id(resolved_repo_id, workspace_path)
        await self._router.set_topic_scope(chat_id, thread_id, scope)
        resolved_workspace_id = (
            resource_id
            if resource_kind == "agent_workspace"
            else self._workspace_id_for_path(workspace_path)
        )
        await self._router.bind_topic(
            chat_id,
            thread_id,
            workspace_path,
            repo_id=resolved_repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_id=resolved_workspace_id,
            scope=scope,
        )

        def apply_bind_updates(record: TelegramTopicRecord) -> None:
            record.resource_kind = resource_kind
            record.resource_id = resource_id
            if resolved_workspace_id:
                record.workspace_id = resolved_workspace_id
            record.pma_enabled = False

        await self._router.update_topic(
            chat_id,
            thread_id,
            apply_bind_updates,
            scope=scope,
        )
        await self._answer_callback(callback, "Bound to repo")
        await self._finalize_selection(
            key,
            callback,
            f"Bound to {resolved_repo_id or workspace_path}.",
        )

    async def _bind_topic_by_repo_id(
        self,
        key: str,
        repo_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        await self._selection_bind_topic_by_repo_id(key, repo_id, callback)

    async def _bind_topic_with_arg(
        self, key: str, arg: str, message: TelegramMessage
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(arg)
        if resolved is None:
            await self._send_message(
                message.chat_id,
                "Unknown repo or path. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace_path, repo_id, resource_kind, resource_id = resolved
        scope = self._topic_scope_id(repo_id, workspace_path)
        await self._router.set_topic_scope(message.chat_id, message.thread_id, scope)
        resolved_workspace_id = (
            resource_id
            if resource_kind == "agent_workspace"
            else self._workspace_id_for_path(workspace_path)
        )
        await self._router.bind_topic(
            message.chat_id,
            message.thread_id,
            workspace_path,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_id=resolved_workspace_id,
            scope=scope,
        )

        def apply_bind_updates(record: TelegramTopicRecord) -> None:
            record.resource_kind = resource_kind
            record.resource_id = resource_id
            if resolved_workspace_id:
                record.workspace_id = resolved_workspace_id
            record.pma_enabled = False

        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            apply_bind_updates,
            scope=scope,
        )
        await self._send_message(
            message.chat_id,
            f"Bound to {repo_id or workspace_path}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
