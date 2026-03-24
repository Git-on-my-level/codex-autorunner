import asyncio
from typing import Any, Optional

from ....app_server.client import CodexAppServerError
from ....chat.approval_modes import (
    APPROVAL_MODE_USAGE,
    normalize_approval_mode,
)
from ...adapter import TelegramMessage
from ...config import AppServerUnavailableError
from ...constants import APPROVAL_POLICY_VALUES
from ...helpers import (
    _clear_policy_overrides,
    _extract_rate_limits,
    _format_persist_note,
    _format_sandbox_policy,
    _set_policy_overrides,
)
from .shared import TelegramCommandSupportMixin


class ApprovalsCommands(TelegramCommandSupportMixin):
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
        argv, persist = self._extract_persist_flag(argv)
        if not argv:
            await self._send_approval_status(message, record)
            return
        mode = normalize_approval_mode(argv[0], include_command_aliases=True)
        if argv[0].lower() == "preset" and len(argv) > 1:
            mode = normalize_approval_mode(argv[1], include_command_aliases=True)
        if mode is not None:
            await self._set_approval_mode(message, mode, persist=persist)
            return
        approval_policy = argv[0] if argv[0] in APPROVAL_POLICY_VALUES else None
        if approval_policy:
            sandbox_policy = argv[1] if len(argv) > 1 else None
            await self._apply_direct_policy(
                message, approval_policy, sandbox_policy, persist=persist
            )
            return
        await self._send_approval_usage(message)

    def _extract_persist_flag(self, argv: list[str]) -> tuple[list[str], bool]:
        """Return argv without the persist flag and whether the flag was provided."""
        if "--persist" not in argv:
            return argv, False
        return [arg for arg in argv if arg != "--persist"], True

    async def _send_approval_status(
        self, message: TelegramMessage, record: Any
    ) -> None:
        """Send the current approval mode and policy to the user."""
        approval_policy, sandbox_policy = self._effective_policies(record)
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    f"Approval mode: {record.approval_mode}",
                    f"Approval policy: {approval_policy or 'default'}",
                    f"Sandbox policy: {_format_sandbox_policy(sandbox_policy)}",
                    f"Usage: /approvals {APPROVAL_MODE_USAGE}",
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _send_approval_usage(self, message: TelegramMessage) -> None:
        """Send the usage hint for the /approvals command."""
        await self._send_message(
            message.chat_id,
            f"Usage: /approvals {APPROVAL_MODE_USAGE}",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _set_approval_mode(
        self, message: TelegramMessage, mode: str, *, persist: bool
    ) -> None:
        """Set a canonical approval mode and clear explicit overrides."""
        await self._router.set_approval_mode(message.chat_id, message.thread_id, mode)
        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            lambda record: _clear_policy_overrides(record),
        )
        await self._send_message(
            message.chat_id,
            _format_persist_note(f"Approval mode set to {mode}.", persist=persist),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _apply_direct_policy(
        self,
        message: TelegramMessage,
        approval_policy: str,
        sandbox_policy: Optional[str],
        *,
        persist: bool,
    ) -> None:
        """Set explicit approval and sandbox policies."""
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
