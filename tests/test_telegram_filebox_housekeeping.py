from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.telegram import service as telegram_service_module
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService


def _config(root: Path) -> TelegramBotConfig:
    return TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
        },
        root=root,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )


@pytest.mark.anyio
async def test_housekeeping_cycle_prunes_fileboxes_using_per_root_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)
    repo_root = tmp_path / "repo"
    workspace_root = tmp_path / "workspace"
    repo_root.mkdir()
    workspace_root.mkdir()
    service._hub_config_path = tmp_path / "codex-autorunner.yml"

    async def _fake_housekeeping_roots() -> list[Path]:
        return [repo_root, workspace_root]

    load_calls: list[tuple[Path, Path | None]] = []
    policy_calls: list[object] = []
    prune_calls: list[tuple[Path, str]] = []
    housekeeping_calls: list[list[Path]] = []
    app_server_pruned = 0
    opencode_pruned = 0

    def _fake_load_repo_config(root: Path, hub_path: Path | None = None):
        load_calls.append((root, hub_path))
        return SimpleNamespace(pma={"root": root.name})

    def _fake_resolve_policy(pma: object) -> str:
        policy_calls.append(pma)
        return f"policy:{pma['root']}"

    def _fake_prune(root: Path, *, policy: str):
        prune_calls.append((root, policy))
        return SimpleNamespace(
            inbox_pruned=0,
            outbox_pruned=0,
            bytes_before=0,
            bytes_after=0,
        )

    def _fake_housekeeping(config: object, roots: list[Path], logger) -> None:
        housekeeping_calls.append(list(roots))

    async def _fake_app_prune_idle() -> None:
        nonlocal app_server_pruned
        app_server_pruned += 1

    async def _fake_opencode_prune_idle() -> None:
        nonlocal opencode_pruned
        opencode_pruned += 1

    monkeypatch.setattr(service, "_housekeeping_roots", _fake_housekeeping_roots)
    monkeypatch.setattr(
        telegram_service_module, "load_repo_config", _fake_load_repo_config
    )
    monkeypatch.setattr(
        telegram_service_module,
        "resolve_filebox_retention_policy",
        _fake_resolve_policy,
    )
    monkeypatch.setattr(telegram_service_module, "prune_filebox_root", _fake_prune)
    monkeypatch.setattr(
        telegram_service_module, "run_housekeeping_for_roots", _fake_housekeeping
    )
    service._app_server_supervisor = SimpleNamespace(prune_idle=_fake_app_prune_idle)
    service._opencode_supervisor = SimpleNamespace(prune_idle=_fake_opencode_prune_idle)

    try:
        await service._run_housekeeping_cycle(
            SimpleNamespace(
                enabled=True,
                interval_seconds=60,
                min_file_age_seconds=0,
                dry_run=False,
                rules=[],
            )
        )
    finally:
        await service._bot.close()

    assert load_calls == [
        (repo_root, service._hub_config_path),
        (workspace_root, service._hub_config_path),
    ]
    assert policy_calls == [{"root": "repo"}, {"root": "workspace"}]
    assert prune_calls == [
        (repo_root, "policy:repo"),
        (workspace_root, "policy:workspace"),
    ]
    assert housekeeping_calls == [[repo_root, workspace_root]]
    assert app_server_pruned == 1
    assert opencode_pruned == 1
