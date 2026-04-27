from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.telegram import service as telegram_service_module
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService
from codex_autorunner.integrations.telegram.state import topic_key


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
    repo_root = tmp_path / "repo"
    workspace_root = tmp_path / "workspace"
    repo_root.mkdir()
    workspace_root.mkdir()

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

    service = TelegramBotService(
        _config(tmp_path),
        hub_root=tmp_path,
        dependencies=telegram_service_module.TelegramServiceDependencies(
            load_repo_config=_fake_load_repo_config,
            resolve_filebox_retention_policy=_fake_resolve_policy,
            prune_filebox_root=_fake_prune,
            run_housekeeping_for_roots=_fake_housekeeping,
        ),
    )
    service._hub_config_path = tmp_path / "codex-autorunner.yml"
    monkeypatch.setattr(service, "_housekeeping_roots", _fake_housekeeping_roots)
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


@pytest.mark.anyio
async def test_gather_workspace_roots_prunes_missing_workspace_bindings(
    tmp_path: Path,
) -> None:
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)
    live_workspace = tmp_path / "live-workspace"
    live_workspace.mkdir()
    missing_workspace = tmp_path / "missing-workspace"
    live_key = topic_key(123, 10)
    missing_key = topic_key(123, 11)

    try:
        await service._store.bind_topic(live_key, str(live_workspace))
        await service._store.bind_topic(missing_key, str(missing_workspace))

        roots = await service._gather_workspace_roots()
        stale_record = await service._store.get_topic(missing_key)
        live_record = await service._store.get_topic(live_key)
    finally:
        await service._bot.close()
        await service._store.close()

    assert roots == [live_workspace.resolve()]
    assert live_record is not None
    assert live_record.workspace_path == str(live_workspace)
    assert stale_record is not None
    assert stale_record.workspace_path is None
    assert stale_record.workspace_id is None


@pytest.mark.anyio
async def test_housekeeping_roots_prune_missing_workspace_bindings(
    tmp_path: Path,
) -> None:
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)
    live_workspace = tmp_path / "housekeeping-live"
    live_workspace.mkdir()
    missing_workspace = tmp_path / "housekeeping-missing"
    missing_key = topic_key(123, 12)

    try:
        await service._store.bind_topic(topic_key(123, 13), str(live_workspace))
        await service._store.bind_topic(missing_key, str(missing_workspace))

        roots = await service._housekeeping_roots()
        stale_record = await service._store.get_topic(missing_key)
    finally:
        await service._bot.close()
        await service._store.close()

    assert roots == sorted([tmp_path.resolve(), live_workspace.resolve()])
    assert stale_record is not None
    assert stale_record.workspace_path is None
    assert stale_record.workspace_id is None


@pytest.mark.anyio
async def test_prewarm_skips_missing_workspaces_without_spawning(
    tmp_path: Path,
) -> None:
    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)
    live_workspace = tmp_path / "prewarm-live"
    live_workspace.mkdir()
    missing_workspace = tmp_path / "prewarm-missing"

    await service._store.bind_topic(topic_key(123, 20), str(live_workspace))
    await service._store.bind_topic(topic_key(123, 21), str(missing_workspace))

    started_roots: list[Path] = []

    async def _fake_get_client(workspace_root: Path) -> object:
        started_roots.append(workspace_root)
        return object()

    service._app_server_supervisor = SimpleNamespace(get_client=_fake_get_client)

    try:
        await service._prewarm_workspace_clients()
    finally:
        await service._bot.close()
        await service._store.close()

    assert started_roots == [live_workspace.resolve()]
