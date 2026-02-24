"""Tests for PMA, Telegram, and chat doctor checks."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.runtime import (
    DoctorCheck,
    hub_worktree_doctor_checks,
    pma_doctor_checks,
)
from codex_autorunner.integrations.chat.doctor import chat_doctor_checks
from codex_autorunner.integrations.chat.parity_checker import ParityCheckResult
from codex_autorunner.integrations.telegram.doctor import (
    telegram_doctor_checks,
)


def test_telegram_doctor_checks_disabled():
    """Test Telegram doctor checks when disabled."""
    checks = telegram_doctor_checks({"telegram_bot": {"enabled": False}})
    assert len(checks) > 0
    assert any(c.check_id == "telegram.enabled" for c in checks)


def test_telegram_doctor_checks_enabled_no_token():
    """Test Telegram doctor checks when enabled but missing token."""
    cfg = {"telegram_bot": {"enabled": True, "bot_token_env": "CAR_TELEGRAM_BOT_TOKEN"}}
    with patch.dict(os.environ, {}, clear=True):
        checks = telegram_doctor_checks(cfg)
    assert len(checks) > 0
    assert any(c.check_id == "telegram.bot_token" for c in checks)


def test_telegram_doctor_checks_mode_validation():
    """Test Telegram doctor checks mode validation."""
    cfg = {"telegram_bot": {"enabled": True, "mode": "invalid"}}
    checks = telegram_doctor_checks(cfg)
    assert len(checks) > 0
    assert any(c.check_id == "telegram.mode" for c in checks)


def test_pma_doctor_checks_disabled():
    """Test PMA doctor checks when disabled."""
    checks = pma_doctor_checks({"pma": {"enabled": False}})
    assert len(checks) > 0
    assert any(c.check_id == "pma.enabled" for c in checks)


def test_pma_doctor_checks_invalid_agent():
    """Test PMA doctor checks with invalid default agent."""
    checks = pma_doctor_checks({"pma": {"enabled": True, "default_agent": "invalid"}})
    assert len(checks) > 0
    assert any(c.check_id == "pma.default_agent" for c in checks)


def test_pma_doctor_checks_missing_state_file(tmp_path: Path):
    """Test PMA doctor checks with missing state file."""
    checks = pma_doctor_checks({"pma": {"enabled": True}}, repo_root=tmp_path)
    assert len(checks) > 0
    state_file_checks = [c for c in checks if c.check_id == "pma.state_file"]
    assert len(state_file_checks) > 0
    assert not state_file_checks[0].passed


def test_doctor_check_to_dict():
    """Test DoctorCheck serialization."""
    check = DoctorCheck(
        name="Test check",
        passed=True,
        message="Test message",
        check_id="test.check",
        severity="info",
        fix="Test fix",
    )
    result = check.to_dict()
    assert result["name"] == "Test check"
    assert result["passed"] is True
    assert result["message"] == "Test message"
    assert result["check_id"] == "test.check"
    assert result["severity"] == "info"
    assert result["fix"] == "Test fix"
    assert result["status"] == "ok"


def test_pma_doctor_checks_missing_config():
    """Test PMA doctor checks with missing config."""
    checks = pma_doctor_checks({})
    assert len(checks) > 0
    assert any(c.check_id == "pma.config" for c in checks)


def test_hub_worktree_doctor_checks_detects_orphans(tmp_path: Path):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    hub_config = load_hub_config(hub_root)
    orphan = hub_config.worktrees_root / "orphan--branch"
    orphan.mkdir(parents=True)
    (orphan / ".git").mkdir()

    checks = hub_worktree_doctor_checks(hub_config)
    assert len(checks) == 1
    check = checks[0]
    assert check.name == "Hub worktrees registered"
    assert check.severity == "warning"
    assert check.passed is False
    assert str(hub_config.worktrees_root) in check.message
    assert f"car hub scan --path {hub_root}" in check.fix
    assert "car hub worktree cleanup" in check.fix


def test_chat_doctor_checks_use_parity_contract_group(monkeypatch):
    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.doctor.run_parity_checks",
        lambda repo_root=None: (
            ParityCheckResult(
                id="discord.contract_commands_routed",
                passed=True,
                message="All routed.",
                metadata={},
            ),
        ),
    )

    checks = chat_doctor_checks()
    assert len(checks) == 1
    check = checks[0]
    assert check.passed is True
    assert check.severity == "info"
    assert check.check_id == "chat.parity_contract"


@pytest.mark.parametrize(
    ("result", "message_snippet", "fix_snippet"),
    [
        (
            ParityCheckResult(
                id="discord.contract_commands_routed",
                passed=False,
                message="missing route",
                metadata={"missing_ids": ["car.model"]},
            ),
            "missing discord command route handling",
            "missing contract commands",
        ),
        (
            ParityCheckResult(
                id="discord.no_generic_fallback_leak",
                passed=False,
                message="fallback leak",
                metadata={"failed_predicates": ["interaction_pma_prefix_guard"]},
            ),
            "known discord command prefixes can leak",
            "command-prefix guards",
        ),
        (
            ParityCheckResult(
                id="discord.canonical_command_ingress_usage",
                passed=False,
                message="shared helper missing",
                metadata={"failed_predicates": ["import_present"]},
            ),
            "shared helper usage for command ingress",
            "canonicalize_command_ingress",
        ),
        (
            ParityCheckResult(
                id="discord.interaction_component_guard_paths",
                passed=False,
                message="guard coverage incomplete",
                metadata={
                    "failed_predicates": ["component_unknown_fallback"],
                },
            ),
            "guard coverage incomplete",
            "align command routing/guard helpers",
        ),
    ],
)
def test_chat_doctor_checks_failures_are_actionable(
    monkeypatch,
    result: ParityCheckResult,
    message_snippet: str,
    fix_snippet: str,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.doctor.run_parity_checks",
        lambda repo_root=None: (result,),
    )

    checks = chat_doctor_checks()
    assert len(checks) == 1
    check = checks[0]
    assert check.passed is False
    assert check.check_id == "chat.parity_contract"
    assert message_snippet in check.message.lower()
    assert check.fix is not None
    assert fix_snippet in check.fix


@pytest.fixture
def tmp_path(tmpdir):
    """Provide a temporary path for testing."""
    return Path(tmpdir)
