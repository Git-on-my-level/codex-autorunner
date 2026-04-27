from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.integrations.discord import doctor as discord_doctor
from codex_autorunner.integrations.discord.doctor import discord_doctor_checks


def _load_hub_with_discord(
    tmp_path: Path,
    discord_bot_cfg: dict[str, object],
    *,
    voice_cfg: dict[str, object] | None = None,
):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    config_path = hub_root / ".codex-autorunner" / "config.yml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    data["discord_bot"] = discord_bot_cfg
    if voice_cfg is not None:
        repo_defaults = data.get("repo_defaults")
        if not isinstance(repo_defaults, dict):
            repo_defaults = {}
            data["repo_defaults"] = repo_defaults
        repo_defaults["voice"] = voice_cfg
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
    assert by_id["discord.invite_url"].passed is True
    assert "2322563695115328" in by_id["discord.invite_url"].message


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

    invite_check = next(
        check for check in checks if check.check_id == "discord.invite_url"
    )
    assert invite_check.passed is True
    assert "client_id=123456" in invite_check.message


def test_discord_doctor_reports_collaboration_policy_summary(
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
            "allowed_guild_ids": ["123"],
        },
    )
    assert isinstance(hub_config.raw, dict)
    hub_config.raw["collaboration_policy"] = {
        "discord": {
            "destinations": [{"guild_id": "123", "channel_id": "456", "mode": "silent"}]
        }
    }

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}
    assert by_id["discord.collaboration_policy"].passed is True
    assert "destinations" in by_id["discord.collaboration_policy"].message
    assert by_id["discord.collaboration_migration"].passed is True
    assert "/car admin ids" in by_id["discord.collaboration_migration"].message


def test_discord_doctor_reports_legacy_collaboration_migration_guidance(
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
            "allowed_guild_ids": ["123"],
        },
    )

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}
    info = by_id["discord.collaboration_migration"]
    assert info.passed is True
    assert "default_mode=command_only" in info.message


def test_discord_doctor_warns_when_destinations_leave_default_mode_active(
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
            "allowed_guild_ids": ["123"],
        },
    )
    assert isinstance(hub_config.raw, dict)
    hub_config.raw["collaboration_policy"] = {
        "discord": {
            "destinations": [{"guild_id": "123", "channel_id": "456", "mode": "active"}]
        }
    }

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}
    warning = by_id["discord.collaboration_policy.default_mode"]
    assert warning.passed is True
    assert warning.severity == "warning"
    assert warning.fix is not None
    assert "command_only" in warning.fix


def test_discord_doctor_checks_legacy_message_content_intent_is_actionable_warning(
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
            "allowed_guild_ids": ["123"],
            "intents": 513,
        },
    )

    checks = discord_doctor_checks(hub_config)
    intents_check = next(
        check for check in checks if check.check_id == "discord.intents"
    )

    assert intents_check.passed is True
    assert intents_check.severity == "warning"
    assert intents_check.fix is not None
    assert "33281" in intents_check.fix


def test_discord_doctor_checks_missing_message_content_intent_is_failure(
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
            "allowed_guild_ids": ["123"],
            "intents": 1,
        },
    )

    checks = discord_doctor_checks(hub_config)
    intents_check = next(
        check for check in checks if check.check_id == "discord.intents"
    )

    assert intents_check.passed is False
    assert intents_check.fix is not None
    assert "32768" in intents_check.fix


def test_discord_doctor_checks_disabled_reports_status(
    tmp_path: Path,
) -> None:
    hub_config = _load_hub_with_discord(tmp_path, {"enabled": False})
    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}

    assert by_id["discord.enabled"].passed is True
    assert "disabled" in by_id["discord.enabled"].message.lower()


def test_discord_doctor_checks_local_voice_missing_dependency_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "token")
    monkeypatch.setenv("TEST_DISCORD_APP_ID", "123456")

    original_missing = discord_doctor.missing_optional_dependencies

    def _missing(deps):
        if deps == (("faster_whisper", "faster-whisper"),):
            return ["faster-whisper"]
        return original_missing(deps)

    monkeypatch.setattr(discord_doctor, "missing_optional_dependencies", _missing)

    hub_config = _load_hub_with_discord(
        tmp_path,
        {
            "enabled": True,
            "bot_token_env": "TEST_DISCORD_TOKEN",
            "app_id_env": "TEST_DISCORD_APP_ID",
            "allowed_guild_ids": ["123"],
        },
        voice_cfg={"enabled": True, "provider": "local_whisper"},
    )

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}
    voice_check = by_id["discord.voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert voice_check.fix is not None
    assert "voice-local" in voice_check.fix


def test_discord_doctor_checks_openai_voice_skips_local_dependency_check(
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
            "allowed_guild_ids": ["123"],
        },
        voice_cfg={"enabled": True, "provider": "openai_whisper"},
    )

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}
    assert "discord.voice.dependencies" not in by_id


def test_discord_doctor_checks_mlx_voice_missing_dependency_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "token")
    monkeypatch.setenv("TEST_DISCORD_APP_ID", "123456")

    original_missing = discord_doctor.missing_optional_dependencies

    def _missing(deps):
        if deps == (("mlx_whisper", "mlx-whisper"),):
            return ["mlx-whisper"]
        return original_missing(deps)

    monkeypatch.setattr(discord_doctor, "missing_optional_dependencies", _missing)

    hub_config = _load_hub_with_discord(
        tmp_path,
        {
            "enabled": True,
            "bot_token_env": "TEST_DISCORD_TOKEN",
            "app_id_env": "TEST_DISCORD_APP_ID",
            "allowed_guild_ids": ["123"],
        },
        voice_cfg={"enabled": True, "provider": "mlx_whisper"},
    )

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}
    voice_check = by_id["discord.voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert voice_check.fix is not None
    assert "voice-mlx" in voice_check.fix


def test_discord_doctor_checks_voice_missing_ffmpeg_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "token")
    monkeypatch.setenv("TEST_DISCORD_APP_ID", "123456")

    original_missing = discord_doctor.missing_optional_dependencies

    def _missing(deps):
        if deps == (("mlx_whisper", "mlx-whisper"),):
            return []
        return original_missing(deps)

    monkeypatch.setattr(discord_doctor, "missing_optional_dependencies", _missing)
    monkeypatch.setattr(
        discord_doctor,
        "missing_local_voice_runtime_commands",
        lambda provider: ["ffmpeg"],
    )

    hub_config = _load_hub_with_discord(
        tmp_path,
        {
            "enabled": True,
            "bot_token_env": "TEST_DISCORD_TOKEN",
            "app_id_env": "TEST_DISCORD_APP_ID",
            "allowed_guild_ids": ["123"],
        },
        voice_cfg={"enabled": True, "provider": "mlx_whisper"},
    )

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}
    voice_check = by_id["discord.voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert "ffmpeg" in voice_check.message
    assert voice_check.fix is not None
    assert "ffmpeg" in voice_check.fix


def test_discord_doctor_checks_voice_disabled_skips_local_dependency_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "token")
    monkeypatch.setenv("TEST_DISCORD_APP_ID", "123456")

    original_missing = discord_doctor.missing_optional_dependencies

    def _missing(deps):
        if deps == (("faster_whisper", "faster-whisper"),):
            return ["faster-whisper"]
        return original_missing(deps)

    monkeypatch.setattr(discord_doctor, "missing_optional_dependencies", _missing)

    hub_config = _load_hub_with_discord(
        tmp_path,
        {
            "enabled": True,
            "bot_token_env": "TEST_DISCORD_TOKEN",
            "app_id_env": "TEST_DISCORD_APP_ID",
            "allowed_guild_ids": ["123"],
        },
        voice_cfg={"enabled": False, "provider": "local_whisper"},
    )

    checks = discord_doctor_checks(hub_config)
    by_id = {check.check_id: check for check in checks}
    assert "discord.voice.dependencies" not in by_id
