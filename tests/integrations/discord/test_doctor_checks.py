from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.integrations.discord.doctor import discord_doctor_checks


def _load_hub_with_discord(tmp_path: Path, discord_bot_cfg: dict[str, object]):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    config_path = hub_root / ".codex-autorunner" / "config.yml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    data["discord_bot"] = discord_bot_cfg
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    return load_hub_config(hub_root)


def test_discord_doctor_checks_missing_env_vars_are_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("TEST_DISCORD_APP_ID", raising=False)

    hub_config = _load_hub_with_discord(
        tmp_path,
        {
            "enabled": True,
            "bot_token_env": "TEST_DISCORD_TOKEN",
            "app_id_env": "TEST_DISCORD_APP_ID",
            "allowed_guild_ids": ["123"],
        },
    )

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}

    assert by_id["discord.bot_token"].passed is False
    assert "TEST_DISCORD_TOKEN" in by_id["discord.bot_token"].message
    assert by_id["discord.app_id"].passed is False
    assert "TEST_DISCORD_APP_ID" in by_id["discord.app_id"].message


def test_discord_doctor_checks_empty_allowlists_is_actionable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "token")
    monkeypatch.setenv("TEST_DISCORD_APP_ID", "123456")

    hub_config = _load_hub_with_discord(
        tmp_path,
        {
            "enabled": True,
            "bot_token_env": "TEST_DISCORD_TOKEN",
            "app_id_env": "TEST_DISCORD_APP_ID",
            "allowed_guild_ids": [],
            "allowed_channel_ids": [],
            "allowed_user_ids": [],
        },
    )

    checks = discord_doctor_checks(hub_config)
    allowlist_check = next(
        check for check in checks if check.check_id == "discord.allowlists"
    )

    assert allowlist_check.passed is False
    assert allowlist_check.fix is not None
    assert "Configure at least one" in allowlist_check.fix


def test_discord_doctor_checks_disabled_reports_status(
    tmp_path: Path,
) -> None:
    hub_config = _load_hub_with_discord(tmp_path, {"enabled": False})
    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}

    assert by_id["discord.enabled"].passed is True
    assert "disabled" in by_id["discord.enabled"].message.lower()
