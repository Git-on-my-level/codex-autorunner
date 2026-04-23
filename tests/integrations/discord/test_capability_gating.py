"""Tests for capability-driven messaging and selector payload in Discord integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.agents.registry import (
    get_agent_descriptor,
    get_registered_agents,
)
from codex_autorunner.integrations.chat.agents import CHAT_AGENT_DEFINITIONS

pytestmark = pytest.mark.integration


def _discord_test_config(root: Path):
    from codex_autorunner.integrations.discord.config import (
        DiscordBotConfig,
        DiscordCommandRegistration,
    )

    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN",
        app_id_env="APP",
        bot_token="token",
        application_id="app",
        allowed_guild_ids=frozenset(),
        allowed_channel_ids=frozenset(),
        allowed_user_ids=frozenset(),
        command_registration=DiscordCommandRegistration(
            enabled=True,
            scope="guild",
            guild_ids=(),
        ),
        state_file=root / "state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=False,
    )


class TestAgentPickerIncludesHermes:
    def test_chat_agent_definitions_include_hermes(self) -> None:
        values = [d.value for d in CHAT_AGENT_DEFINITIONS]
        assert "hermes" in values

    def test_registered_agents_include_hermes(self) -> None:
        agents = get_registered_agents()
        assert "hermes" in agents
        descriptor = agents["hermes"]
        assert descriptor.id == "hermes"
        assert descriptor.name == "Hermes"

    def test_hermes_has_expected_capabilities(self) -> None:
        descriptor = get_agent_descriptor("hermes")
        assert descriptor is not None
        caps = descriptor.capabilities
        assert "message_turns" in caps
        assert "durable_threads" in caps
        assert "interrupt" in caps
        assert "event_streaming" in caps
        assert "approvals" in caps

    def test_hermes_lacks_model_listing(self) -> None:
        descriptor = get_agent_descriptor("hermes")
        assert descriptor is not None
        caps = descriptor.capabilities
        assert "model_listing" not in caps

    def test_hermes_lacks_review(self) -> None:
        descriptor = get_agent_descriptor("hermes")
        assert descriptor is not None
        caps = descriptor.capabilities
        assert "review" not in caps

    def test_codex_has_model_listing(self) -> None:
        descriptor = get_agent_descriptor("codex")
        assert descriptor is not None
        caps = descriptor.capabilities
        assert "model_listing" in caps

    def test_codex_has_review(self) -> None:
        descriptor = get_agent_descriptor("codex")
        assert descriptor is not None
        caps = descriptor.capabilities
        assert "review" in caps

    def test_opencode_has_model_listing(self) -> None:
        descriptor = get_agent_descriptor("opencode")
        assert descriptor is not None
        caps = descriptor.capabilities
        assert "model_listing" in caps

    def test_opencode_has_review(self) -> None:
        descriptor = get_agent_descriptor("opencode")
        assert descriptor is not None
        caps = descriptor.capabilities
        assert "review" in caps


class TestCapabilityHelpers:
    def test_agent_supports_capability_for_hermes(self, tmp_path: Path) -> None:
        import logging

        from codex_autorunner.integrations.discord.service import DiscordBotService

        config = _discord_test_config(tmp_path)

        service = DiscordBotService(
            config,
            logger=logging.getLogger("test"),
        )

        assert service._agent_supports_capability("hermes", "message_turns") is True
        assert (
            service._agent_supports_capability("hermes", "conversation_compaction")
            is True
        )
        assert service._agent_supports_capability("hermes", "session_resume") is True
        assert service._agent_supports_capability("hermes", "turn_control") is True
        assert service._agent_supports_capability("hermes", "model_listing") is False
        assert service._agent_supports_capability("hermes", "review") is False
        assert service._agent_supports_capability("codex", "model_listing") is True
        assert service._agent_supports_capability("codex", "review") is True
        assert service._agent_supports_capability("opencode", "model_listing") is True
        assert service._agent_supports_capability("opencode", "review") is True

    def test_agents_supporting_capability_returns_correct_agents(
        self, tmp_path: Path
    ) -> None:
        import logging

        from codex_autorunner.integrations.discord.service import DiscordBotService

        config = _discord_test_config(tmp_path)

        service = DiscordBotService(
            config,
            logger=logging.getLogger("test"),
        )

        model_listing_agents = service._agents_supporting_capability("model_listing")
        assert "codex" in model_listing_agents
        assert "opencode" in model_listing_agents
        assert "hermes" not in model_listing_agents

        review_agents = service._agents_supporting_capability("review")
        assert "codex" in review_agents
        assert "opencode" in review_agents
        assert "hermes" not in review_agents

        message_turns_agents = service._agents_supporting_capability("message_turns")
        assert "codex" in message_turns_agents
        assert "opencode" in message_turns_agents
        assert "hermes" in message_turns_agents

    def test_agent_display_name_returns_correct_name(self, tmp_path: Path) -> None:
        import logging

        from codex_autorunner.integrations.discord.service import DiscordBotService

        config = _discord_test_config(tmp_path)

        service = DiscordBotService(
            config,
            logger=logging.getLogger("test"),
        )

        assert service._agent_display_name("hermes") == "Hermes"
        assert service._agent_display_name("codex") == "Codex"
        assert service._agent_display_name("opencode") == "OpenCode"

    def test_agent_supports_resume_uses_capability(self, tmp_path: Path) -> None:
        import logging

        from codex_autorunner.integrations.discord.service import DiscordBotService

        config = _discord_test_config(tmp_path)

        service = DiscordBotService(
            config,
            logger=logging.getLogger("test"),
        )

        assert service._agent_supports_resume("codex") is True
        assert service._agent_supports_resume("opencode") is True
        assert service._agent_supports_resume("hermes") is True
        assert service._agent_supports_resume("zeroclaw") is True

    def test_agent_supports_effort_uses_chat_capability_helper(
        self, tmp_path: Path
    ) -> None:
        import logging

        from codex_autorunner.integrations.discord.service import DiscordBotService

        config = _discord_test_config(tmp_path)

        service = DiscordBotService(
            config,
            logger=logging.getLogger("test"),
        )

        assert service._agent_supports_effort("codex") is True
        assert service._agent_supports_effort("opencode") is True
        assert service._agent_supports_effort("hermes") is False


class TestWebAgentSelectorIncludesHermes:
    def test_available_agents_uses_registry(self) -> None:
        from unittest.mock import MagicMock, patch

        from codex_autorunner.surfaces.web.routes.agents import _available_agents

        registered = get_registered_agents()

        class FakeConfig:
            def agent_binary(self, agent_id: str) -> str:
                return agent_id

        class FakeState:
            def __init__(self) -> None:
                self.app_server_supervisor = MagicMock()
                self.opencode_supervisor = MagicMock()
                self.config = FakeConfig()
                self.zeroclaw_supervisor = None
                self.hermes_supervisor = MagicMock()
                for agent_id in registered.keys():
                    setattr(self, f"{agent_id}_supervisor", MagicMock())
                self.logger = None

        request = MagicMock()
        request.app.state = FakeState()

        with patch(
            "codex_autorunner.agents.registry.get_available_agents",
            wraps=lambda app_state: registered,
        ):
            agents, default = _available_agents(request)

        agent_ids = {a["id"] for a in agents}
        assert "hermes" in agent_ids
        assert "codex" in agent_ids
        assert "opencode" in agent_ids
