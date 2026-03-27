"""Tests for capability-aware filtering in PMA CLI."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from codex_autorunner.surfaces.cli.pma_cli import (
    _CAPABILITY_REQUIREMENTS,
    _check_capability,
    _fetch_agent_capabilities,
    _normalize_agent_option,
    pma_app,
)

runner = CliRunner()


class TestCapabilityRequirements:
    def test_models_requires_model_listing(self):
        assert _CAPABILITY_REQUIREMENTS.get("models") == "model_listing"

    def test_interrupt_requires_interrupt(self):
        assert _CAPABILITY_REQUIREMENTS.get("interrupt") == "interrupt"

    def test_thread_interrupt_requires_interrupt(self):
        assert _CAPABILITY_REQUIREMENTS.get("thread_interrupt") == "interrupt"

    def test_thread_spawn_requires_durable_threads(self):
        assert _CAPABILITY_REQUIREMENTS.get("thread_spawn") == "durable_threads"

    def test_thread_turns_requires_transcript_history(self):
        assert _CAPABILITY_REQUIREMENTS.get("thread_turns") == "transcript_history"

    def test_thread_tail_requires_event_streaming(self):
        assert _CAPABILITY_REQUIREMENTS.get("thread_tail") == "event_streaming"


class TestCheckCapability:
    def test_check_capability_returns_true_when_present(self):
        capabilities = {"codex": ["durable_threads", "interrupt", "message_turns"]}
        assert _check_capability("codex", "interrupt", capabilities) is True

    def test_check_capability_returns_false_when_missing(self):
        capabilities = {"codex": ["durable_threads", "message_turns"]}
        assert _check_capability("codex", "interrupt", capabilities) is False

    def test_check_capability_returns_false_for_unknown_agent(self):
        capabilities = {"codex": ["durable_threads", "interrupt"]}
        assert _check_capability("unknown_agent", "interrupt", capabilities) is False

    def test_check_capability_handles_empty_capabilities(self):
        capabilities = {}
        assert _check_capability("codex", "interrupt", capabilities) is False


class TestFetchAgentCapabilities:
    @patch("codex_autorunner.surfaces.cli.pma_cli._request_json")
    @patch("codex_autorunner.surfaces.cli.pma_cli.load_hub_config")
    def test_fetch_agent_capabilities_returns_mapping(self, mock_config, mock_request):
        mock_config.return_value = MagicMock()
        mock_request.return_value = {
            "agents": [
                {"id": "codex", "capabilities": ["durable_threads", "interrupt"]},
                {"id": "opencode", "capabilities": ["durable_threads"]},
            ]
        }
        config = mock_config.return_value
        result = _fetch_agent_capabilities(config)
        assert result == {
            "codex": ["durable_threads", "interrupt"],
            "opencode": ["durable_threads"],
        }

    @patch("codex_autorunner.surfaces.cli.pma_cli._request_json")
    @patch("codex_autorunner.surfaces.cli.pma_cli.load_hub_config")
    def test_fetch_agent_capabilities_handles_error(self, mock_config, mock_request):
        mock_config.return_value = MagicMock()
        mock_request.side_effect = Exception("Network error")
        config = mock_config.return_value
        result = _fetch_agent_capabilities(config)
        assert result == {}

    @patch("codex_autorunner.surfaces.cli.pma_cli._request_json")
    @patch("codex_autorunner.surfaces.cli.pma_cli.load_hub_config")
    def test_fetch_agent_capabilities_handles_missing_agents(
        self, mock_config, mock_request
    ):
        mock_config.return_value = MagicMock()
        mock_request.return_value = {}
        config = mock_config.return_value
        result = _fetch_agent_capabilities(config)
        assert result == {}


class TestPmaModelsCapabilityCheck:
    @patch("codex_autorunner.surfaces.cli.pma_cli._fetch_agent_capabilities")
    @patch("codex_autorunner.surfaces.cli.pma_cli.load_hub_config")
    @patch("codex_autorunner.surfaces.cli.pma_cli._build_pma_url")
    @patch("codex_autorunner.surfaces.cli.pma_cli._request_json")
    def test_models_command_fails_for_agent_without_model_listing(
        self, mock_request, mock_url, mock_config, mock_caps
    ):
        mock_config.return_value = MagicMock()
        mock_caps.return_value = {"codex": ["durable_threads", "interrupt"]}
        mock_url.return_value = "http://localhost:8080/hub/pma/agents/codex/models"
        mock_request.side_effect = Exception("Should not reach here")

        result = runner.invoke(pma_app, ["models", "codex"])
        assert result.exit_code == 1
        assert "does not support model listing" in result.output
        assert "model_listing" in result.output


class TestPmaThreadSpawnCapabilityCheck:
    @patch("codex_autorunner.surfaces.cli.pma_cli._fetch_agent_capabilities")
    @patch("codex_autorunner.surfaces.cli.pma_cli.load_hub_config")
    def test_thread_spawn_fails_for_agent_without_durable_threads(
        self, mock_config, mock_caps
    ):
        mock_config.return_value = MagicMock()
        mock_caps.return_value = {"codex": ["message_turns", "interrupt"]}

        result = runner.invoke(
            pma_app,
            [
                "thread",
                "spawn",
                "--agent",
                "codex",
                "--repo",
                "test-repo",
            ],
        )
        assert result.exit_code == 1
        assert "does not support thread creation" in result.output
        assert "durable_threads" in result.output


class TestNormalizeAgentOption:
    def test_normalize_agent_option_returns_none_for_none(self):
        result = _normalize_agent_option(None)
        assert result is None

    def test_normalize_agent_option_returns_none_for_blank(self):
        result = _normalize_agent_option("   ")
        assert result is None

    def test_normalize_agent_option_accepts_registered_agents(self):
        result = _normalize_agent_option("CODEX")
        assert result == "codex"

        result = _normalize_agent_option("  OpenCode  ")
        assert result == "opencode"

    def test_normalize_agent_option_accepts_hermes(self):
        result = _normalize_agent_option("hermes")
        assert result == "hermes"

        result = _normalize_agent_option("HERMES")
        assert result == "hermes"

    def test_normalize_agent_option_rejects_unknown_agent(self):
        result = runner.invoke(
            pma_app,
            [
                "thread",
                "spawn",
                "--agent",
                "unknown-agent",
                "--repo",
                "test-repo",
            ],
        )
        assert result.exit_code == 1
        assert "registered agent" in result.output
