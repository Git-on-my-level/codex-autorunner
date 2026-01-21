import asyncio
from typing import Any, Optional

from ....app_server.client import CodexAppServerError
from ...adapter import TelegramMessage
from ...config import AppServerUnavailableError
from ...constants import APPROVAL_POLICY_VALUES, APPROVAL_PRESETS
from ...helpers import (
    _clear_policy_overrides,
    _extract_rate_limits,
    _format_persist_note,
    _format_sandbox_policy,
    _normalize_approval_preset,
    _set_policy_overrides,
)


class ApprovalsCommands:
    async def _read_rate_limits(
        self, workspace_path: Optional[str], *, agent: str
    ) -> Optional[dict[str, Any]]:
        if self._agent_rate_limit_source(agent) != "app_server":
            return None
        try:
            client = await self._client_for_workspace(workspace_path)
        except AppServerUnavailableError:
            return None
        if client is None:
            return None
        for method in ("account/rateLimits/read", "account/read"):
            try:
                result = await client.request(method, params=None, timeout=5.0)
            except (CodexAppServerError, asyncio.TimeoutError):
                continue
            rate_limits = _extract_rate_limits(result)
            if rate_limits:
                return rate_limits
        return None

    async def _handle_approvals(
        self, message: TelegramMessage, args: str, _runtime: Optional[Any] = None
    ) -> None:
        argv = self._parse_command_args(args)
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        if not argv:
            approval_policy, sandbox_policy = self._effective_policies(record)
            await self._send_message(
                message.chat_id,
                "\n".join(
                    [
                        f"Approval mode: {record.approval_mode}",
                        f"Approval policy: {approval_policy or 'default'}",
                        f"Sandbox policy: {_format_sandbox_policy(sandbox_policy)}",
                        "Usage: /approvals yolo|safe|read-only|auto|full-access",
                    ]
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        persist = False
        if "--persist" in argv:
            persist = True
            argv = [arg for arg in argv if arg != "--persist"]
        if not argv:
            await self._send_message(
                message.chat_id,
                "Usage: /approvals yolo|safe|read-only|auto|full-access",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        mode = argv[0].lower()
        if mode in ("yolo", "off", "disable", "disabled"):
            await self._router.set_approval_mode(
                message.chat_id, message.thread_id, "yolo"
            )
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _clear_policy_overrides(record),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note("Approval mode set to yolo.", persist=persist),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if mode in ("safe", "on", "enable", "enabled"):
            await self._router.set_approval_mode(
                message.chat_id, message.thread_id, "safe"
            )
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _clear_policy_overrides(record),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note("Approval mode set to safe.", persist=persist),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        preset = _normalize_approval_preset(mode)
        if mode == "preset" and len(argv) > 1:
            preset = _normalize_approval_preset(argv[1])
        if preset:
            approval_policy, sandbox_policy = APPROVAL_PRESETS[preset]
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_policy_overrides(
                    record,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                ),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note(
                    f"Approval policy set to {approval_policy} with sandbox {sandbox_policy}.",
                    persist=persist,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        approval_policy = argv[0] if argv[0] in APPROVAL_POLICY_VALUES else None
        if approval_policy:
            sandbox_policy = argv[1] if len(argv) > 1 else None
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_policy_overrides(
                    record,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                ),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note(
                    f"Approval policy set to {approval_policy} with sandbox {sandbox_policy or 'default'}.",
                    persist=persist,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Usage: /approvals yolo|safe|read-only|auto|full-access",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
