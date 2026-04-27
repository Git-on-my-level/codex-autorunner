from pathlib import Path

import pytest

import codex_autorunner.integrations.telegram.service as service_module
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService


@pytest.mark.anyio
async def test_spawned_task_tracking(tmp_path: Path) -> None:
    config = TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
        },
        root=tmp_path,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )
    service = TelegramBotService(config)
    try:

        async def _noop() -> None:
            return None

        task = service._spawn_task(_noop())
        await task
        assert task not in service._spawned_tasks
    finally:
        await service._bot.close()


@pytest.mark.anyio
async def test_spawned_task_failure_is_logged(tmp_path: Path, monkeypatch) -> None:
    config = TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
        },
        root=tmp_path,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )
    service = TelegramBotService(config)
    events: list[tuple[int, str, dict[str, object]]] = []

    def _capture_log_event(_logger, level, event_name: str, **kwargs: object) -> None:
        events.append((level, event_name, kwargs))

    service._dependencies = service_module.TelegramServiceDependencies(
        load_repo_config=service._dependencies.load_repo_config,
        resolve_filebox_retention_policy=(
            service._dependencies.resolve_filebox_retention_policy
        ),
        prune_filebox_root=service._dependencies.prune_filebox_root,
        run_housekeeping_for_roots=service._dependencies.run_housekeeping_for_roots,
        reap_managed_processes=service._dependencies.reap_managed_processes,
        log_event=_capture_log_event,
    )
    try:

        async def _boom() -> None:
            raise RuntimeError("boom")

        task = service._spawn_task(_boom())
        with pytest.raises(RuntimeError, match="boom"):
            await task
        assert any(name == "telegram.task.failed" for _, name, _ in events)
    finally:
        await service._bot.close()
