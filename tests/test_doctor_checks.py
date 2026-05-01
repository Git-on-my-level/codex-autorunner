"""Tests for PMA, Telegram, and chat doctor checks."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.agent_config import AgentConfig
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.destinations import DockerReadiness
from codex_autorunner.core.managed_processes.registry import ProcessRecord
from codex_autorunner.core.runtime import (
    DoctorCheck,
    doctor,
    hermes_doctor_checks,
    hub_destination_doctor_checks,
    hub_worktree_doctor_checks,
    pma_doctor_checks,
    summarize_opencode_lifecycle,
    zeroclaw_doctor_checks,
)
from codex_autorunner.integrations.chat.doctor import (
    ChatSurfaceLabContractStatus,
    chat_doctor_checks,
    chat_surface_lab_doctor_checks,
)
from codex_autorunner.integrations.chat.parity_checker import ParityCheckResult
from codex_autorunner.integrations.telegram import doctor as telegram_doctor
from codex_autorunner.integrations.telegram.doctor import (
    telegram_doctor_checks,
)
from tests.conftest import write_test_config


def _tg_cfg(**overrides):
    cfg: dict = {
        "telegram_bot": {
            "enabled": True,
            "allowed_chat_ids": [-1001],
            "allowed_user_ids": [42],
        }
    }
    cfg.update(overrides)
    return cfg


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


def test_telegram_doctor_reports_collaboration_policy_summary():
    cfg = _tg_cfg(
        collaboration_policy={
            "telegram": {
                "destinations": [
                    {"chat_id": -1001, "thread_id": 7, "mode": "command_only"}
                ]
            }
        },
    )
    checks = telegram_doctor_checks(cfg)
    by_id = {check.check_id: check for check in checks}
    assert by_id["telegram.collaboration_policy"].passed is True
    assert "destinations" in by_id["telegram.collaboration_policy"].message


def test_telegram_doctor_reports_collaboration_migration_guidance():
    cfg = _tg_cfg()
    checks = telegram_doctor_checks(cfg)
    by_id = {check.check_id: check for check in checks}
    assert by_id["telegram.collaboration_migration"].passed is True
    assert "/bind or /pma on" in by_id["telegram.collaboration_migration"].message


def test_telegram_doctor_reports_mentions_privacy_guidance():
    cfg = _tg_cfg()
    cfg["telegram_bot"]["trigger_mode"] = "mentions"
    checks = telegram_doctor_checks(cfg)
    by_id = {check.check_id: check for check in checks}
    assert by_id["telegram.privacy_mode_guidance"].passed is True
    assert "privacy mode" in by_id["telegram.privacy_mode_guidance"].message.lower()


def test_telegram_doctor_warns_when_root_chat_remains_active():
    cfg = _tg_cfg(
        collaboration_policy={
            "telegram": {
                "default_mode": "active",
                "destinations": [
                    {
                        "chat_id": -1001,
                        "thread_id": 7,
                        "mode": "active",
                    }
                ],
            }
        },
    )
    checks = telegram_doctor_checks(cfg)
    by_id = {check.check_id: check for check in checks}
    assert by_id["telegram.collaboration_policy.root_chat"].passed is False
    assert "root chat" in by_id["telegram.collaboration_policy.root_chat"].message


def test_telegram_doctor_reports_configured_collaboration_migration_guidance():
    cfg = _tg_cfg(
        collaboration_policy={
            "telegram": {
                "destinations": [
                    {
                        "chat_id": -1001,
                        "thread_id": 7,
                        "mode": "active",
                    }
                ]
            }
        },
    )
    checks = telegram_doctor_checks(cfg)
    by_id = {check.check_id: check for check in checks}
    assert by_id["telegram.collaboration_migration"].passed is True
    assert "/status" in by_id["telegram.collaboration_migration"].message


def test_telegram_doctor_checks_local_voice_dependency_missing(monkeypatch):
    """Test Telegram doctor check catches missing local whisper dependency."""

    def _missing_optional(deps):
        if deps == (("httpx", "httpx"),):
            return []
        if deps == (("faster_whisper", "faster-whisper"),):
            return ["faster-whisper"]
        return []

    monkeypatch.setattr(
        telegram_doctor, "missing_optional_dependencies", _missing_optional
    )
    cfg = {
        "telegram_bot": {"enabled": True},
        "voice": {"enabled": True, "provider": "local_whisper"},
    }
    with patch.dict(os.environ, {}, clear=True):
        checks = telegram_doctor_checks(cfg)

    by_id = {check.check_id: check for check in checks}
    voice_check = by_id["telegram.voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert voice_check.fix is not None
    assert "voice-local" in voice_check.fix


def test_telegram_doctor_checks_mlx_voice_dependency_missing(monkeypatch):
    """Test Telegram doctor check catches missing MLX whisper dependency."""

    def _missing_optional(deps):
        if deps == (("httpx", "httpx"),):
            return []
        if deps == (("mlx_whisper", "mlx-whisper"),):
            return ["mlx-whisper"]
        return []

    monkeypatch.setattr(
        telegram_doctor, "missing_optional_dependencies", _missing_optional
    )
    cfg = {
        "telegram_bot": {"enabled": True},
        "voice": {"enabled": True, "provider": "mlx_whisper"},
    }
    with patch.dict(os.environ, {}, clear=True):
        checks = telegram_doctor_checks(cfg)

    by_id = {check.check_id: check for check in checks}
    voice_check = by_id["telegram.voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert voice_check.fix is not None
    assert "voice-mlx" in voice_check.fix


def test_telegram_doctor_checks_voice_missing_ffmpeg(monkeypatch):
    """Test Telegram doctor check catches missing ffmpeg runtime command."""

    def _missing_optional(deps):
        if deps == (("httpx", "httpx"),):
            return []
        if deps == (("mlx_whisper", "mlx-whisper"),):
            return []
        return []

    monkeypatch.setattr(
        telegram_doctor, "missing_optional_dependencies", _missing_optional
    )
    monkeypatch.setattr(
        telegram_doctor,
        "missing_local_voice_runtime_commands",
        lambda provider: ["ffmpeg"],
    )
    cfg = {
        "telegram_bot": {"enabled": True},
        "voice": {"enabled": True, "provider": "mlx_whisper"},
    }
    with patch.dict(os.environ, {}, clear=True):
        checks = telegram_doctor_checks(cfg)

    by_id = {check.check_id: check for check in checks}
    voice_check = by_id["telegram.voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert "ffmpeg" in voice_check.message
    assert voice_check.fix is not None
    assert "ffmpeg" in voice_check.fix


def test_telegram_doctor_checks_openai_voice_skips_local_dependency_check(monkeypatch):
    """Test Telegram doctor check skips local deps when provider is OpenAI."""

    def _missing_optional(deps):
        if deps == (("httpx", "httpx"),):
            return []
        return ["faster-whisper"]

    monkeypatch.setattr(
        telegram_doctor, "missing_optional_dependencies", _missing_optional
    )
    cfg = {
        "telegram_bot": {"enabled": True},
        "voice": {"enabled": True, "provider": "openai_whisper"},
    }
    with patch.dict(os.environ, {}, clear=True):
        checks = telegram_doctor_checks(cfg)

    by_id = {check.check_id: check for check in checks}
    assert "telegram.voice.dependencies" not in by_id


def test_pma_doctor_checks_disabled():
    """Test PMA doctor checks when disabled."""
    checks = pma_doctor_checks({"pma": {"enabled": False}})
    assert len(checks) > 0
    assert any(c.check_id == "pma.enabled" for c in checks)


def test_doctor_reports_missing_local_voice_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test core doctor reports missing local whisper deps when voice is enabled."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    monkeypatch.setattr(
        "codex_autorunner.core.git_utils.run_git",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    def _missing_optional(deps):
        if deps == (("faster_whisper", "faster-whisper"),):
            return ["faster-whisper"]
        return []

    monkeypatch.setattr(
        "codex_autorunner.core.runtime.missing_optional_dependencies",
        _missing_optional,
    )

    write_test_config(
        hub_root / ".codex-autorunner" / "config.yml",
        {
            "mode": "hub",
            "repo_defaults": {"voice": {"enabled": True, "provider": "local_whisper"}},
        },
    )
    with patch.dict(os.environ, {}, clear=True):
        report = doctor(hub_root)

    by_id = {check.check_id: check for check in report.checks if check.check_id}
    voice_check = by_id["voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert voice_check.fix is not None
    assert "voice-local" in voice_check.fix


def test_doctor_reports_missing_mlx_voice_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test core doctor reports missing MLX deps when voice is enabled."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    monkeypatch.setattr(
        "codex_autorunner.core.git_utils.run_git",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    def _missing_optional(deps):
        if deps == (("mlx_whisper", "mlx-whisper"),):
            return ["mlx-whisper"]
        return []

    monkeypatch.setattr(
        "codex_autorunner.core.runtime.missing_optional_dependencies",
        _missing_optional,
    )

    write_test_config(
        hub_root / ".codex-autorunner" / "config.yml",
        {
            "mode": "hub",
            "repo_defaults": {"voice": {"enabled": True, "provider": "mlx_whisper"}},
        },
    )
    with patch.dict(os.environ, {}, clear=True):
        report = doctor(hub_root)

    by_id = {check.check_id: check for check in report.checks if check.check_id}
    voice_check = by_id["voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert voice_check.fix is not None
    assert "voice-mlx" in voice_check.fix


def test_doctor_reports_missing_mlx_voice_runtime_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test core doctor reports missing ffmpeg for local MLX voice."""
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    monkeypatch.setattr(
        "codex_autorunner.core.git_utils.run_git",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    def _missing_optional(deps):
        if deps == (("mlx_whisper", "mlx-whisper"),):
            return []
        return []

    monkeypatch.setattr(
        "codex_autorunner.core.runtime.missing_optional_dependencies",
        _missing_optional,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.runtime.missing_local_voice_runtime_commands",
        lambda provider: ["ffmpeg"],
    )

    write_test_config(
        hub_root / ".codex-autorunner" / "config.yml",
        {
            "mode": "hub",
            "repo_defaults": {"voice": {"enabled": True, "provider": "mlx_whisper"}},
        },
    )
    with patch.dict(os.environ, {}, clear=True):
        report = doctor(hub_root)

    by_id = {check.check_id: check for check in report.checks if check.check_id}
    voice_check = by_id["voice.dependencies"]
    assert voice_check.passed is False
    assert voice_check.severity == "error"
    assert "ffmpeg" in voice_check.message
    assert voice_check.fix is not None
    assert "ffmpeg" in voice_check.fix


def test_doctor_reports_render_browser_dependency_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    monkeypatch.setattr(
        "codex_autorunner.core.git_utils.run_git",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr("codex_autorunner.core.runtime.find_spec", lambda _name: None)

    report = doctor(hub_root)
    render_checks = [
        check
        for check in report.checks
        if check.check_id == "render.browser.dependencies"
    ]
    assert len(render_checks) == 1
    render_check = render_checks[0]
    assert render_check.passed is True
    assert "Playwright Python package is not installed" in render_check.message
    assert render_check.fix is not None
    assert "codex-autorunner[browser]" in render_check.fix


def test_doctor_reports_render_markdown_downstream_tool_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    monkeypatch.setattr(
        "codex_autorunner.core.git_utils.run_git",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr("codex_autorunner.core.runtime.find_spec", lambda _name: None)
    monkeypatch.setattr(
        "codex_autorunner.core.runtime.resolve_executable", lambda _name: None
    )

    report = doctor(hub_root)
    messages = [check.message for check in report.checks]
    assert any("Mermaid CLI (mmdc) is not installed" in msg for msg in messages)
    assert any("Pandoc is not installed" in msg for msg in messages)


def test_summarize_opencode_lifecycle_dedupes_pid_records_and_reports_handle_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_config = SimpleNamespace(
        agents={"opencode": SimpleNamespace(base_url=None)},
        opencode=SimpleNamespace(server_scope="workspace"),
    )
    workspace_record = ProcessRecord(
        kind="opencode",
        workspace_id="ws-1",
        pid=4201,
        pgid=4201,
        base_url="http://127.0.0.1:4201",
        command=["opencode", "serve"],
        owner_pid=111,
        started_at="2026-03-01T00:00:00Z",
        metadata={
            "workspace_root": str(tmp_path / "ws-1"),
            "server_scope": "workspace",
            "process_origin": "spawned_local",
            "last_attach_mode": "registry_reuse",
        },
    )
    pid_record = ProcessRecord(
        kind="opencode",
        workspace_id=None,
        pid=4201,
        pgid=4201,
        base_url="http://127.0.0.1:4201",
        command=["opencode", "serve"],
        owner_pid=111,
        started_at="2026-03-01T00:00:00Z",
        metadata={
            "workspace_root": str(tmp_path / "ws-1"),
            "workspace_id": "ws-1",
            "server_scope": "workspace",
            "process_origin": "spawned_local",
            "last_attach_mode": "spawned_local",
        },
    )
    stale_record = ProcessRecord(
        kind="opencode",
        workspace_id="ws-2",
        pid=4202,
        pgid=4202,
        base_url="http://127.0.0.1:4202",
        command=["opencode", "serve"],
        owner_pid=222,
        started_at="2026-03-01T00:00:00Z",
        metadata={
            "workspace_root": str(tmp_path / "ws-2"),
            "server_scope": "workspace",
            "process_origin": "spawned_local",
            "last_attach_mode": "spawned_local",
        },
    )

    monkeypatch.setattr(
        "codex_autorunner.core.runtime.list_process_records",
        lambda *_args, **_kwargs: [pid_record, stale_record, workspace_record],
    )
    monkeypatch.setattr(
        "codex_autorunner.core.runtime._opencode_record_is_running",
        lambda record: record.pid == 4201,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.runtime._pid_exists",
        lambda pid: pid == 111,
    )

    class _StubSupervisor:
        def observability_snapshot(self) -> dict[str, object]:
            return {
                "handles": [
                    {
                        "workspace_id": "ws-1",
                        "mode": "managed_registry_reuse",
                        "active_turns": 0,
                        "process_pid": None,
                        "managed_pid": 4201,
                        "base_url": "http://127.0.0.1:4201",
                    },
                    {
                        "workspace_id": "ws-3",
                        "mode": "managed_spawned",
                        "active_turns": 1,
                        "process_pid": 4300,
                        "managed_pid": 4300,
                        "base_url": "http://127.0.0.1:4300",
                    },
                    {
                        "workspace_id": "ws-4",
                        "mode": "external_base_url",
                        "active_turns": 0,
                        "process_pid": None,
                        "managed_pid": None,
                        "base_url": "http://external:7777",
                    },
                ]
            }

    backend_orchestrator = SimpleNamespace(
        _active_backend=SimpleNamespace(_supervisor=_StubSupervisor())
    )

    summary = summarize_opencode_lifecycle(
        tmp_path,
        repo_config=repo_config,
        backend_orchestrator=backend_orchestrator,
    )

    assert summary["counts"] == {
        "active": 1,
        "stale": 1,
        "spawned_local": 2,
        "registry_reuse": 1,
    }
    assert len(summary["managed_servers"]) == 2
    assert summary["managed_servers"][0]["workspace_id"] == "ws-2"
    assert summary["managed_servers"][1]["workspace_id"] == "ws-1"
    assert summary["managed_servers"][1]["owner_alive"] is True
    assert [handle["mode"] for handle in summary["live_handles"]] == [
        "managed_registry_reuse",
        "managed_spawned",
        "external_base_url",
    ]


def test_doctor_reports_opencode_external_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    monkeypatch.setattr(
        "codex_autorunner.core.git_utils.run_git",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.runtime.load_repo_config",
        lambda _repo_root: SimpleNamespace(
            voice={},
            agents={"opencode": SimpleNamespace(base_url="http://external:7777")},
            opencode=SimpleNamespace(server_scope="workspace"),
        ),
    )
    monkeypatch.setattr(
        "codex_autorunner.core.runtime.list_process_records",
        lambda *_args, **_kwargs: [],
    )

    report = doctor(hub_root)

    by_id = {check.check_id: check for check in report.checks if check.check_id}
    external_check = by_id["opencode.lifecycle.external"]
    registry_check = by_id["opencode.lifecycle.registry"]
    assert external_check.passed is True
    assert "External OpenCode base_url configured" in external_check.message
    assert registry_check.passed is True
    assert "No CAR-managed OpenCode server records found" in registry_check.message


def test_pma_doctor_checks_invalid_agent():
    """Test PMA doctor checks with invalid default agent."""
    checks = pma_doctor_checks({"pma": {"enabled": True, "default_agent": "invalid"}})
    assert len(checks) > 0
    invalid_agent_checks = [c for c in checks if c.check_id == "pma.default_agent"]
    assert len(invalid_agent_checks) == 1
    assert not invalid_agent_checks[0].passed
    assert invalid_agent_checks[0].fix is not None
    assert "registered agent" in invalid_agent_checks[0].fix.lower()


def test_pma_doctor_checks_valid_registered_agent():
    """Test PMA doctor checks with a valid registered agent."""
    checks = pma_doctor_checks({"pma": {"enabled": True, "default_agent": "hermes"}})
    assert len(checks) > 0
    agent_checks = [c for c in checks if c.check_id == "pma.default_agent"]
    assert len(agent_checks) == 1
    assert agent_checks[0].passed
    assert agent_checks[0].message == "Default agent: hermes"


def test_pma_doctor_checks_reports_alias_backed_default_agent_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    hub_config = load_hub_config(hub_root)
    hub_config.pma.default_agent = "hermes-m4-pma"
    hub_config.raw.setdefault("pma", {})["default_agent"] = "hermes-m4-pma"
    hub_config.agents["hermes-m4-pma"] = AgentConfig(
        backend="hermes",
        binary="hermes-m4-pma",
        serve_command=None,
        base_url=None,
        subagent_models=None,
    )

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        lambda _config, *, agent_id="hermes", profile=None: type(
            "Result",
            (),
            {
                "status": "ready",
                "message": f"Hermes {agent_id} ready.",
                "fix": None,
                "version": None,
                "launch_mode": None,
            },
        )(),
    )

    checks = pma_doctor_checks(hub_config)

    readiness = [c for c in checks if c.check_id == "pma.default_agent.runtime"]
    assert len(readiness) == 1
    assert readiness[0].passed is True
    assert "hermes-m4-pma" in readiness[0].message


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


def test_hub_destination_doctor_checks_reports_effective_destination(
    tmp_path: Path, monkeypatch
):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                "  - id: base",
                "    path: workspace/base",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
                "    destination:",
                "      kind: docker",
                "      image: ghcr.io/acme/base:latest",
                "  - id: wt",
                "    path: worktrees/wt",
                "    enabled: true",
                "    auto_run: false",
                "    kind: worktree",
                "    worktree_of: base",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    hub_config = load_hub_config(hub_root)
    monkeypatch.setattr(
        "codex_autorunner.core.runtime.probe_docker_readiness",
        lambda: DockerReadiness(
            binary_available=True,
            daemon_reachable=True,
            detail="docker daemon reachable",
        ),
    )
    checks = hub_destination_doctor_checks(hub_config)
    assert any(
        "base: effective destination 'docker'" in check.message for check in checks
    )
    assert any(
        "wt: effective destination 'docker' (source=base)" in check.message
        for check in checks
    )
    assert all(check.passed for check in checks)


def test_hub_destination_doctor_checks_reports_daemon_unreachable(
    tmp_path: Path, monkeypatch
):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                "  - id: base",
                "    path: workspace/base",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
                "    destination:",
                "      kind: docker",
                "      image: ghcr.io/acme/base:latest",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "codex_autorunner.core.runtime.probe_docker_readiness",
        lambda: DockerReadiness(
            binary_available=True,
            daemon_reachable=False,
            detail="Cannot connect to the Docker daemon",
        ),
    )
    hub_config = load_hub_config(hub_root)
    checks = hub_destination_doctor_checks(hub_config)
    daemon_checks = [
        check for check in checks if check.check_id == "hub.destination.docker.daemon"
    ]
    assert len(daemon_checks) == 1
    daemon_check = daemon_checks[0]
    assert daemon_check.passed is False
    assert daemon_check.severity == "warning"
    assert "Cannot connect to the Docker daemon" in daemon_check.message


def test_hub_destination_doctor_checks_reports_invalid_destination(tmp_path: Path):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                "  - id: bad",
                "    path: workspace/bad",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
                "    destination:",
                "      kind: docker",
                "      image: 123",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    hub_config = load_hub_config(hub_root)
    checks = hub_destination_doctor_checks(hub_config)
    assert any(
        "bad: effective destination 'local' (source=default)" in check.message
        for check in checks
    )
    assert any(
        (not check.passed) and "requires non-empty 'image'" in check.message
        for check in checks
    )


def test_hub_destination_doctor_checks_reports_agent_workspace_destination(
    tmp_path: Path, monkeypatch
):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos: []",
                "agent_workspaces:",
                "  - id: zc-main",
                "    runtime: zeroclaw",
                "    path: .codex-autorunner/runtimes/zeroclaw/zc-main",
                "    enabled: true",
                "    destination:",
                "      kind: docker",
                "      image: ghcr.io/acme/zeroclaw:latest",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    hub_config = load_hub_config(hub_root)
    monkeypatch.setattr(
        "codex_autorunner.core.runtime.probe_docker_readiness",
        lambda: DockerReadiness(
            binary_available=True,
            daemon_reachable=True,
            detail="docker daemon reachable",
        ),
    )
    checks = hub_destination_doctor_checks(hub_config)
    assert any(
        "zc-main: effective destination 'docker' (source=agent_workspace)"
        in check.message
        for check in checks
    )
    assert any(
        check.check_id == "hub.destination.docker.daemon"
        and "agent_workspace:zc-main" in check.message
        for check in checks
    )


def test_hub_destination_doctor_checks_reports_invalid_agent_workspace_destination(
    tmp_path: Path,
):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos: []",
                "agent_workspaces:",
                "  - id: zc-main",
                "    runtime: zeroclaw",
                "    path: .codex-autorunner/runtimes/zeroclaw/zc-main",
                "    enabled: true",
                "    destination:",
                "      kind: docker",
                "      image: ''",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    hub_config = load_hub_config(hub_root)
    checks = hub_destination_doctor_checks(hub_config)
    assert any(
        "zc-main: effective destination 'local' (source=default)" in check.message
        for check in checks
    )
    assert any(
        (not check.passed) and "requires non-empty 'image'" in check.message
        for check in checks
    )


def test_zeroclaw_doctor_checks_report_missing_binary_for_enabled_workspaces(
    tmp_path: Path, monkeypatch
):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos: []",
                "agent_workspaces:",
                "  - id: zc-main",
                "    runtime: zeroclaw",
                "    path: .codex-autorunner/runtimes/zeroclaw/zc-main",
                "    enabled: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    hub_config = load_hub_config(hub_root)
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.zeroclaw_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "missing_binary",
                "version": None,
                "launch_mode": None,
                "message": "ZeroClaw binary 'zeroclaw' is not available on PATH.",
                "fix": "Install ZeroClaw on the host.",
            },
        )(),
    )

    checks = zeroclaw_doctor_checks(hub_config)

    assert len(checks) == 1
    check = checks[0]
    assert check.check_id == "hub.zeroclaw.binary"
    assert check.passed is False
    assert check.severity == "error"
    assert "zc-main" in check.message
    assert "Install ZeroClaw" in (check.fix or "")


def test_zeroclaw_doctor_checks_report_incompatible_runtime_for_enabled_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos: []",
                "agent_workspaces:",
                "  - id: zc-main",
                "    runtime: zeroclaw",
                "    path: .codex-autorunner/runtimes/zeroclaw/zc-main",
                "    enabled: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    hub_config = load_hub_config(hub_root)
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.zeroclaw_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "incompatible",
                "version": "zeroclaw 0.2.0",
                "launch_mode": None,
                "message": "ZeroClaw zeroclaw 0.2.0 does not advertise `zeroclaw agent --session-state-file` in `zeroclaw agent --help`.",
                "fix": "Install a compatible ZeroClaw build.",
            },
        )(),
    )

    checks = zeroclaw_doctor_checks(hub_config)

    assert len(checks) == 1
    check = checks[0]
    assert check.passed is False
    assert check.severity == "error"
    assert "session-state-file" in check.message
    assert "zc-main" in check.message


def test_zeroclaw_doctor_checks_skip_default_binary_without_workspaces(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    hub_config = load_hub_config(hub_root)

    assert zeroclaw_doctor_checks(hub_config) == []


def test_hermes_doctor_checks_skip_default_binary_without_workspaces(tmp_path: Path):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    hub_config = load_hub_config(hub_root)

    assert hermes_doctor_checks(hub_config) == []


def test_hermes_doctor_checks_report_missing_binary_for_enabled_workspaces(
    tmp_path: Path, monkeypatch
):
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos: []",
                "agent_workspaces:",
                "  - id: hermes-main",
                "    runtime: hermes",
                "    path: .codex-autorunner/runtimes/hermes/hermes-main",
                "    enabled: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    hub_config = load_hub_config(hub_root)
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "missing_binary",
                "version": None,
                "launch_mode": None,
                "message": "Hermes binary 'hermes' is not available on PATH.",
                "fix": "Install Hermes on the host.",
            },
        )(),
    )

    checks = hermes_doctor_checks(hub_config)

    assert len(checks) == 1
    check = checks[0]
    assert check.check_id == "hub.hermes.binary"
    assert check.passed is False
    assert check.severity == "error"
    assert "hermes-main" in check.message
    assert "Install Hermes" in (check.fix or "")


def test_hermes_doctor_checks_report_incompatible_runtime_for_enabled_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.write_text(
        "\n".join(
            [
                "version: 3",
                "repos: []",
                "agent_workspaces:",
                "  - id: hermes-main",
                "    runtime: hermes",
                "    path: .codex-autorunner/runtimes/hermes/hermes-main",
                "    enabled: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    hub_config = load_hub_config(hub_root)
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        lambda _config: type(
            "Result",
            (),
            {
                "status": "incompatible",
                "version": "hermes 0.1.0",
                "launch_mode": None,
                "message": "Hermes hermes 0.1.0 does not advertise `hermes acp` in `hermes --help`.",
                "fix": "Install a compatible Hermes build.",
            },
        )(),
    )

    checks = hermes_doctor_checks(hub_config)

    assert len(checks) == 1
    check = checks[0]
    assert check.passed is False
    assert check.severity == "error"
    assert "hermes acp" in check.message
    assert "hermes-main" in check.message


def test_hermes_doctor_checks_report_configured_aliases_individually(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    hub_config = load_hub_config(hub_root)
    hub_config.agents["hermes-m4-pma"] = AgentConfig(
        backend="hermes",
        binary="hermes-m4-pma",
        serve_command=None,
        base_url=None,
        subagent_models=None,
    )

    observed_agent_ids: list[str] = []

    def _fake_preflight(_config, *, agent_id="hermes", profile=None):
        observed_agent_ids.append(agent_id)
        return type(
            "Result",
            (),
            {
                "status": "ready",
                "version": "hermes 1.2.3",
                "launch_mode": None,
                "message": f"Hermes {agent_id} ready.",
                "fix": None,
            },
        )()

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        _fake_preflight,
    )

    checks = hermes_doctor_checks(hub_config)

    assert observed_agent_ids == ["hermes-m4-pma"]
    assert len(checks) == 1
    assert checks[0].check_id == "hub.hermes.binary.hermes-m4-pma"
    assert checks[0].passed is True
    assert "hermes-m4-pma" in checks[0].message


def test_hermes_doctor_detects_prefix_alias_missing_backend_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    hub_config = load_hub_config(hub_root)
    hub_config.agents["hermes-m4-pma"] = AgentConfig(
        backend=None,
        binary="hermes-m4-pma",
        serve_command=None,
        base_url=None,
        subagent_models=None,
    )

    observed_agent_ids: list[str] = []

    def _fake_preflight(_config, *, agent_id="hermes", profile=None):
        observed_agent_ids.append(agent_id)
        return type(
            "Result",
            (),
            {
                "status": "ready",
                "version": "hermes 1.2.3",
                "launch_mode": None,
                "message": f"Hermes {agent_id} ready.",
                "fix": None,
            },
        )()

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.hermes_runtime_preflight",
        _fake_preflight,
    )

    checks = hermes_doctor_checks(hub_config)

    assert observed_agent_ids == ["hermes-m4-pma"]
    metadata = [
        c for c in checks if c.check_id == "hub.hermes.alias_metadata.hermes-m4-pma"
    ]
    assert len(metadata) == 1
    assert metadata[0].severity == "warning"
    assert metadata[0].passed is False
    assert "backend: hermes" in metadata[0].message
    runtime = [c for c in checks if c.check_id == "hub.hermes.binary.hermes-m4-pma"]
    assert len(runtime) == 1
    assert runtime[0].passed is True


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
                id="contract.registry_entries_cataloged",
                passed=False,
                message="registry missing",
                metadata={
                    "missing_discord_paths": ["car:bind"],
                    "missing_telegram_commands": ["bind"],
                },
            ),
            "command registry coverage is incomplete",
            "Update `COMMAND_CONTRACT`",
        ),
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


def test_chat_doctor_checks_report_ux_regression_contract_failures(monkeypatch):
    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.doctor.run_parity_checks",
        lambda repo_root=None: (
            ParityCheckResult(
                id="chat.ux_latency_budget_contract_complete",
                passed=False,
                message="missing budgets",
                metadata={
                    "missing_required_budget_ids": ["queue_visible"],
                    "invalid_budget_ids": [],
                    "duplicate_budget_ids": [],
                },
            ),
            ParityCheckResult(
                id="chat.ux_regression_contract_complete",
                passed=False,
                message="missing scenarios",
                metadata={
                    "missing_required_scenario_ids": ["queued_visibility"],
                    "missing_test_paths": [],
                    "scenarios_missing_surface_coverage": ["duplicate_delivery"],
                    "scenarios_with_missing_budget_refs": [],
                    "scenarios_with_empty_tests": [],
                    "duplicate_scenario_ids": [],
                },
            ),
        ),
    )

    checks = chat_doctor_checks()
    by_name = {check.name: check for check in checks}

    budget_check = by_name[
        "Chat parity contract (chat.ux_latency_budget_contract_complete)"
    ]
    coverage_check = by_name[
        "Chat parity contract (chat.ux_regression_contract_complete)"
    ]

    assert budget_check.passed is False
    assert "queue_visible" in budget_check.message
    assert budget_check.fix is not None
    assert "ux_regression_contract.py" in budget_check.fix

    assert coverage_check.passed is False
    assert "queued_visibility" in coverage_check.message
    assert coverage_check.fix is not None
    assert "concrete regression tests" in coverage_check.fix


def test_chat_surface_lab_doctor_checks_report_contract_health(monkeypatch) -> None:
    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.doctor._collect_chat_surface_lab_contract_status",
        lambda repo_root=None: ChatSurfaceLabContractStatus(
            scenario_count=9,
        ),
    )

    checks = chat_surface_lab_doctor_checks()
    by_id = {check.check_id: check for check in checks}

    scenario_check = by_id["chat.surface_lab.scenario_corpus"]
    contract_check = by_id["chat.surface_lab.contract_links"]
    artifact_check = by_id["chat.surface_lab.artifact_contract"]

    assert scenario_check.passed is True
    assert "9 files" in scenario_check.message
    assert scenario_check.fix is None
    assert contract_check.passed is True
    assert contract_check.fix is None
    assert artifact_check.passed is True
    assert artifact_check.fix is None


def test_chat_surface_lab_doctor_checks_failures_are_actionable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.doctor._collect_chat_surface_lab_contract_status",
        lambda repo_root=None: ChatSurfaceLabContractStatus(
            scenario_count=7,
            parse_errors=("first_visible_feedback.json:missing scenario_id",),
            missing_required_scenarios=("queued_visibility",),
            missing_regression_links=("interrupt_confirmation",),
            unknown_regression_links=("ghost_regression",),
            missing_latency_budget_links=("queue_visible",),
            unknown_latency_budget_links=("unexpected_budget",),
            missing_latency_budget_assertions=("interrupt_visible",),
            missing_artifact_kinds=("timing_report_json",),
            missing_artifact_filenames=("timing_report.json",),
        ),
    )

    checks = chat_surface_lab_doctor_checks()
    by_id = {check.check_id: check for check in checks}

    scenario_check = by_id["chat.surface_lab.scenario_corpus"]
    contract_check = by_id["chat.surface_lab.contract_links"]
    artifact_check = by_id["chat.surface_lab.artifact_contract"]

    assert scenario_check.passed is False
    assert "missing_scenarios=queued_visibility" in scenario_check.message
    assert scenario_check.fix is not None
    assert "make test-chat-surface-lab" in scenario_check.fix

    assert contract_check.passed is False
    assert "unknown_regression_links=ghost_regression" in contract_check.message
    assert (
        "missing_latency_budget_assertions=interrupt_visible" in contract_check.message
    )
    assert contract_check.fix is not None
    assert "contract_links" in contract_check.fix

    assert artifact_check.passed is False
    assert "timing_report_json" in artifact_check.message
    assert "timing_report.json" in artifact_check.message
    assert artifact_check.fix is not None
    assert "evidence_artifacts.py" in artifact_check.fix


def test_chat_surface_lab_doctor_checks_skip_when_checkout_paths_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.integrations.chat.doctor._collect_chat_surface_lab_contract_status",
        lambda repo_root=None: ChatSurfaceLabContractStatus(
            skipped=True,
            skip_reason="missing checkout paths: tests/chat_surface_lab/scenarios",
        ),
    )

    checks = chat_surface_lab_doctor_checks()
    assert len(checks) == 1
    check = checks[0]
    assert check.passed is True
    assert check.severity == "info"
    assert check.check_id == "chat.surface_lab.skipped"
    assert "missing checkout paths" in check.message


@pytest.fixture
def tmp_path(tmpdir):
    """Provide a temporary path for testing."""
    return Path(tmpdir)
