from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.chat.approval_modes import (
    APPROVAL_MODE_POLICIES,
    APPROVAL_MODE_USAGE,
    APPROVAL_MODE_VALUES,
)
from codex_autorunner.integrations.discord.message_turns import (
    _resolve_discord_turn_policies,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.config import TelegramBotDefaults
from codex_autorunner.integrations.telegram.handlers.commands.approvals import (
    ApprovalsCommands,
)
from codex_autorunner.integrations.telegram.handlers.commands.workspace import (
    WorkspaceCommands,
)
from codex_autorunner.integrations.telegram.state import (
    TelegramTopicRecord,
    normalize_approval_mode,
)


def _telegram_defaults(
    *,
    approval_policy: str | None = "on-request",
    sandbox_policy: str | None = "workspaceWrite",
    yolo_approval_policy: str = "never",
    yolo_sandbox_policy: str = "dangerFullAccess",
) -> TelegramBotDefaults:
    return TelegramBotDefaults(
        approval_mode="yolo",
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
        yolo_approval_policy=yolo_approval_policy,
        yolo_sandbox_policy=yolo_sandbox_policy,
    )


@pytest.mark.parametrize("mode", APPROVAL_MODE_VALUES)
def test_discord_and_telegram_share_approval_policy_contract(mode: str) -> None:
    expected = APPROVAL_MODE_POLICIES[mode]

    assert (
        _resolve_discord_turn_policies(
            {"approval_mode": mode},
            default_approval_policy="never",
            default_sandbox_policy="dangerFullAccess",
        )
        == expected
    )
    assert (
        WorkspaceCommands._effective_policies(
            SimpleNamespace(_config=SimpleNamespace(defaults=_telegram_defaults())),
            TelegramTopicRecord(approval_mode=mode),
        )
        == expected
    )


@pytest.mark.parametrize("mode", APPROVAL_MODE_VALUES)
def test_telegram_normalize_approval_mode_accepts_shared_modes(mode: str) -> None:
    assert normalize_approval_mode(mode) == mode


class _DiscordStoreStub:
    async def get_binding(self, *, channel_id: str) -> dict[str, str]:
        assert channel_id == "channel-1"
        return {
            "approval_mode": "safe",
            "approval_policy": "on-request",
            "sandbox_policy": "workspaceWrite",
        }


class _DiscordApprovalsStub:
    def __init__(self) -> None:
        self._store = _DiscordStoreStub()
        self.responses: list[str] = []

    def interaction_has_initial_response(self, interaction_token: str) -> bool:
        assert interaction_token == "token-1"
        return False

    def interaction_is_deferred(self, interaction_token: str) -> bool:
        assert interaction_token == "token-1"
        return False

    async def respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        assert interaction_id == "interaction-1"
        assert interaction_token == "token-1"
        self.responses.append(text)


class _TelegramRouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self.record = record

    async def ensure_topic(
        self, _chat_id: int, _thread_id: int | None
    ) -> TelegramTopicRecord:
        return self.record

    async def set_approval_mode(
        self, _chat_id: int, _thread_id: int | None, mode: str
    ) -> TelegramTopicRecord:
        self.record.approval_mode = mode
        return self.record

    async def update_topic(self, _chat_id: int, _thread_id: int | None, apply):
        apply(self.record)
        return self.record


class _TelegramApprovalsStub(ApprovalsCommands):
    def __init__(self, record: TelegramTopicRecord) -> None:
        self.__dict__["_router"] = _TelegramRouterStub(record)
        self._config = SimpleNamespace(defaults=_telegram_defaults())
        self.messages: list[str] = []

    @property
    def _router(self) -> _TelegramRouterStub:
        return self.__dict__["_router"]

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        reply_markup: object = None,
    ) -> None:
        _ = thread_id, reply_to, reply_markup
        self.messages.append(text)

    def _effective_policies(
        self, record: TelegramTopicRecord
    ) -> tuple[str | None, object | None]:
        return WorkspaceCommands._effective_policies(
            SimpleNamespace(_config=SimpleNamespace(defaults=_telegram_defaults())),
            record,
        )


def test_telegram_effective_policies_honor_configured_safe_defaults() -> None:
    assert WorkspaceCommands._effective_policies(
        SimpleNamespace(
            _config=SimpleNamespace(
                defaults=_telegram_defaults(
                    approval_policy="on-request",
                    sandbox_policy="readOnly",
                )
            )
        ),
        TelegramTopicRecord(approval_mode="safe"),
    ) == ("on-request", "readOnly")


def test_telegram_effective_policies_honor_configured_yolo_defaults() -> None:
    assert WorkspaceCommands._effective_policies(
        SimpleNamespace(
            _config=SimpleNamespace(
                defaults=_telegram_defaults(
                    yolo_approval_policy="never",
                    yolo_sandbox_policy="workspaceWrite",
                )
            )
        ),
        TelegramTopicRecord(approval_mode="yolo"),
    ) == ("never", "workspaceWrite")


def _telegram_message() -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/approvals",
        date=0,
        is_topic_message=True,
    )


@pytest.mark.anyio
async def test_discord_approval_status_uses_shared_usage_string() -> None:
    service = _DiscordApprovalsStub()

    await DiscordBotService._handle_car_approvals(
        service,
        "interaction-1",
        "token-1",
        channel_id="channel-1",
        options={},
    )

    assert service.responses == [
        "\n".join(
            [
                "Approval mode: safe",
                "Approval policy: on-request",
                "Sandbox policy: workspaceWrite",
                "",
                f"Usage: /car approvals {APPROVAL_MODE_USAGE}",
            ]
        )
    ]


@pytest.mark.anyio
async def test_telegram_approvals_command_sets_mode_and_clears_overrides() -> None:
    record = TelegramTopicRecord(
        approval_mode="yolo",
        approval_policy="on-failure",
        sandbox_policy="dangerFullAccess",
    )
    commands = _TelegramApprovalsStub(record)

    await commands._handle_approvals(_telegram_message(), "read-only")

    assert record.approval_mode == "read-only"
    assert record.approval_policy is None
    assert record.sandbox_policy is None
    assert commands.messages == ["Approval mode set to read-only."]


@pytest.mark.anyio
async def test_telegram_approval_usage_uses_shared_usage_string() -> None:
    commands = _TelegramApprovalsStub(TelegramTopicRecord())

    await commands._send_approval_usage(_telegram_message())

    assert commands.messages == [f"Usage: /approvals {APPROVAL_MODE_USAGE}"]
