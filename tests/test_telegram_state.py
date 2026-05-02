from pathlib import Path

import pytest

from codex_autorunner.core.chat_bindings import repo_has_active_non_pma_chat_binding
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration.bindings import OrchestrationBindingStore
from codex_autorunner.core.orchestration.sqlite import initialize_orchestration_sqlite
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.integrations.telegram.state import (
    OutboxRecord,
    PendingApprovalRecord,
    TelegramState,
    TelegramStateStore,
    TelegramTopicRecord,
    topic_key,
)
from codex_autorunner.manifest import (
    MANIFEST_VERSION,
    Manifest,
    ManifestRepo,
    save_manifest,
)
from tests.conftest import write_test_config


@pytest.mark.anyio
async def test_telegram_state_global_update_id(tmp_path: Path) -> None:
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        assert await store.get_last_update_id_global() is None
        assert await store.update_last_update_id_global(10) == 10
        assert await store.get_last_update_id_global() == 10
        assert await store.update_last_update_id_global(3) == 10
    finally:
        await store.close()


@pytest.mark.anyio
async def test_telegram_state_json_path_with_sqlite(tmp_path: Path) -> None:
    """
    Guard against regressions where a SQLite-backed state file uses a
    `.json` suffix. The store should initialize cleanly regardless of
    the file extension.
    """

    path = tmp_path / "telegram_state.json"
    store = TelegramStateStore(path)
    try:
        records = await store.list_pending_voice()
        assert records == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_telegram_state_migrates_legacy_json_on_first_open(
    tmp_path: Path,
) -> None:
    key = topic_key(123, 456)
    legacy_state = TelegramState(
        topics={
            key: TelegramTopicRecord(
                workspace_path=str(tmp_path / "workspace"),
                active_thread_id="thread-1",
                last_update_id=99,
            )
        },
        topic_scopes={key: "repo:example"},
        pending_approvals={
            "approval-1": PendingApprovalRecord(
                request_id="approval-1",
                turn_id="turn-1",
                chat_id=123,
                thread_id=456,
                message_id=789,
                prompt="approve?",
                created_at="2026-05-02T00:00:00+00:00",
                topic_key=key,
            )
        },
        outbox={
            "outbox-1": OutboxRecord(
                record_id="outbox-1",
                chat_id=123,
                thread_id=456,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text="pending",
                created_at="2026-05-02T00:00:00+00:00",
            )
        },
        last_update_id_global=42,
    )
    (tmp_path / "telegram_state.json").write_text(
        legacy_state.to_json(),
        encoding="utf-8",
    )

    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    try:
        migrated = await store.load()
    finally:
        await store.close()

    assert migrated.topics[key].active_thread_id == "thread-1"
    assert migrated.topic_scopes[key] == "repo:example"
    assert migrated.pending_approvals["approval-1"].turn_id == "turn-1"
    assert migrated.outbox["outbox-1"].text == "pending"
    assert migrated.last_update_id_global == 42


@pytest.mark.anyio
async def test_telegram_state_pma_toggle(tmp_path: Path) -> None:
    store = TelegramStateStore(tmp_path / "telegram_state.sqlite3")
    key = topic_key(123, None)
    try:
        record = await store.ensure_topic(key)
        assert record.pma_enabled is False
        record = await store.update_topic(
            key, lambda record: setattr(record, "pma_enabled", True)
        )
        assert record.pma_enabled is True
        record = await store.get_topic(key)
        assert record is not None
        assert record.pma_enabled is True
    finally:
        await store.close()


@pytest.mark.anyio
async def test_telegram_state_no_longer_needs_to_be_binding_authority(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = DEFAULT_HUB_CONFIG.copy()
    write_test_config(hub_root / CONFIG_FILENAME, cfg)
    workspace_root = (hub_root / "worktrees" / "repo-1").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    manifest_path = (hub_root / ".codex-autorunner" / "manifest.yml").resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    save_manifest(
        manifest_path,
        Manifest(
            version=MANIFEST_VERSION,
            repos=[
                ManifestRepo(
                    id="repo-1", path=Path("worktrees/repo-1"), kind="worktree"
                )
            ],
        ),
        hub_root,
    )
    initialize_orchestration_sqlite(hub_root)
    thread = PmaThreadStore(hub_root).create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        name="Telegram thread",
    )
    OrchestrationBindingStore(hub_root).upsert_binding(
        surface_kind="telegram",
        surface_key=topic_key(123, None),
        thread_target_id=str(thread["managed_thread_id"]),
        agent_id="codex",
        repo_id="repo-1",
    )
    store = TelegramStateStore(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    )
    try:
        record = await store.bind_topic(
            topic_key(123, None), str(workspace_root), repo_id="repo-1"
        )
        assert record.active_thread_id is None
        assert (
            repo_has_active_non_pma_chat_binding(
                hub_root=hub_root,
                raw_config=cfg,
                repo_id="repo-1",
            )
            is True
        )
    finally:
        await store.close()


def test_telegram_topic_record_from_dict_migrates_legacy_hermes_alias() -> None:
    record = TelegramTopicRecord.from_dict(
        {"agent": "hermes-m4-pma"},
        default_approval_mode="yolo",
    )

    assert record.agent == "hermes"
    assert record.agent_profile == "m4-pma"
