"""Tests for PMA and Telegram doctor checks."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_autorunner.core.runtime import DoctorCheck, pma_doctor_checks
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


@pytest.fixture
def tmp_path(tmpdir):
    """Provide a temporary path for testing."""
    return Path(tmpdir)
