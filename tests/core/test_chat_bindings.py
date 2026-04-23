from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codex_autorunner.core.chat_bindings import (
    active_chat_binding_counts,
    active_chat_binding_counts_by_source,
    preferred_non_pma_chat_notification_source_for_workspace,
    repo_has_active_chat_binding,
    repo_has_active_non_pma_chat_binding,
)
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.orchestration import (
    OrchestrationBindingStore,
    initialize_orchestration_sqlite,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.manifest import (
    MANIFEST_VERSION,
    Manifest,
    ManifestRepo,
    save_manifest,
)
from tests.conftest import write_test_config


def _write_manifest_repo(
    hub_root: Path,
    *,
    repo_id: str,
    relative_path: str,
    manifest_relative_path: str = ".codex-autorunner/manifest.yml",
) -> Path:
    workspace_path = (hub_root / relative_path).resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)
    manifest_path = (hub_root / manifest_relative_path).resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        version=MANIFEST_VERSION,
        repos=[
            ManifestRepo(
                id=repo_id,
                path=Path(relative_path),
                kind="worktree",
            )
        ],
    )
    save_manifest(manifest_path, manifest, hub_root)
    return workspace_path


def _write_discord_binding(
    db_path: Path,
    *,
    channel_id: str,
    repo_id: str | None,
    workspace_path: str | None = None,
    updated_at: str = "2026-03-12T00:00:00Z",
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    channel_id TEXT PRIMARY KEY,
                    workspace_path TEXT,
                    repo_id TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO channel_bindings (
                    channel_id, workspace_path, repo_id, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    workspace_path=excluded.workspace_path,
                    repo_id=excluded.repo_id,
                    updated_at=excluded.updated_at
                """,
                (channel_id, workspace_path, repo_id, updated_at),
            )
    finally:
        conn.close()


def _write_telegram_binding(
    db_path: Path,
    *,
    topic_key: str,
    repo_id: str | None,
    workspace_path: str | None = None,
    updated_at: str = "2026-03-12T00:00:00Z",
    last_active_at: str | None = None,
) -> None:
    if ":" not in topic_key:
        raise ValueError(
            "topic_key must be in '<chat_id>:<thread_or_root>[:scope]' form"
        )
    parts = topic_key.split(":", 2)
    chat_id = int(parts[0])
    thread_raw = parts[1]
    thread_id = None if thread_raw == "root" else int(thread_raw)
    scope = parts[2] if len(parts) == 3 else None

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topics (
                    topic_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    workspace_path TEXT,
                    repo_id TEXT,
                    last_active_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO telegram_topics (
                    topic_key,
                    chat_id,
                    thread_id,
                    scope,
                    workspace_path,
                    repo_id,
                    last_active_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    scope=excluded.scope,
                    workspace_path=excluded.workspace_path,
                    repo_id=excluded.repo_id,
                    last_active_at=excluded.last_active_at,
                    updated_at=excluded.updated_at
                """,
                (
                    topic_key,
                    chat_id,
                    thread_id,
                    scope,
                    workspace_path,
                    repo_id,
                    last_active_at,
                    updated_at,
                ),
            )
    finally:
        conn.close()


def _write_telegram_topic_scope(
    db_path: Path, *, chat_id: int, thread_id: int | None, scope: str | None
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO telegram_topic_scopes (chat_id, thread_id, scope)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET scope=excluded.scope
                """,
                (chat_id, thread_id, scope),
            )
    finally:
        conn.close()


def _write_orchestration_binding(
    hub_root: Path,
    *,
    surface_kind: str,
    surface_key: str,
    repo_id: str,
    workspace_path: str,
    agent_id: str = "codex",
) -> None:
    initialize_orchestration_sqlite(hub_root, durable=False)
    workspace_root = Path(workspace_path)
    workspace_root.mkdir(parents=True, exist_ok=True)
    thread_store = PmaThreadStore(hub_root)
    thread = thread_store.create_thread(
        agent_id,
        workspace_root,
        repo_id=repo_id,
        name=f"{surface_kind} binding",
    )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind=surface_kind,
        surface_key=surface_key,
        thread_target_id=str(thread["managed_thread_id"]),
        agent_id=agent_id,
        repo_id=repo_id,
        mode="reuse",
    )


def test_active_chat_binding_counts_aggregates_persisted_sources(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    thread_store = PmaThreadStore(hub_root)
    thread_store.create_thread(
        "codex",
        (hub_root / "worktrees" / "repo-a-1").resolve(),
        repo_id="repo-a",
    )
    thread_store.create_thread(
        "codex",
        (hub_root / "worktrees" / "repo-a-2").resolve(),
        repo_id="repo-a",
    )

    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-chan-1",
        repo_id="repo-a",
    )
    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-chan-2",
        repo_id="repo-b",
    )
    _write_telegram_binding(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topic_key="123:root",
        repo_id="repo-b",
    )

    counts = active_chat_binding_counts(hub_root=hub_root, raw_config=cfg)
    assert counts["repo-a"] == 3
    assert counts["repo-b"] == 2

    counts_by_source = active_chat_binding_counts_by_source(
        hub_root=hub_root, raw_config=cfg
    )
    assert counts_by_source["repo-a"] == {"pma": 2, "discord": 1}
    assert counts_by_source["repo-b"] == {"discord": 1, "telegram": 1}


def test_repo_has_active_chat_binding_uses_configured_state_files(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["discord_bot"]["state_file"] = "state/custom-discord.sqlite3"
    cfg["telegram_bot"]["state_file"] = "state/custom-telegram.sqlite3"
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    discord_db = hub_root / "state" / "custom-discord.sqlite3"
    telegram_db = hub_root / "state" / "custom-telegram.sqlite3"
    _write_discord_binding(discord_db, channel_id="discord-chan", repo_id="repo-x")
    _write_telegram_binding(telegram_db, topic_key="999:root", repo_id="repo-y")
    thread_store = PmaThreadStore(hub_root)
    thread_store.create_thread(
        "codex",
        (hub_root / "worktrees" / "repo-pma-only").resolve(),
        repo_id="repo-pma-only",
    )

    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root, raw_config=cfg, repo_id="repo-x"
        )
        is True
    )
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root, raw_config=cfg, repo_id="repo-y"
        )
        is True
    )
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root, raw_config=cfg, repo_id="repo-z"
        )
        is False
    )
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-pma-only",
            include_pma=False,
        )
        is False
    )
    assert (
        repo_has_active_non_pma_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-pma-only",
        )
        is False
    )
    assert (
        repo_has_active_non_pma_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-x",
        )
        is True
    )


def test_telegram_binding_lookup_ignores_non_current_scoped_topics(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    telegram_db = hub_root / ".codex-autorunner" / "telegram_state.sqlite3"
    _write_telegram_topic_scope(
        telegram_db, chat_id=200, thread_id=17, scope="scope-current"
    )
    _write_telegram_binding(
        telegram_db,
        topic_key="200:17:scope-old",
        repo_id="repo-stale",
    )
    _write_telegram_binding(
        telegram_db,
        topic_key="200:17:scope-current",
        repo_id="repo-current",
    )

    counts = active_chat_binding_counts(hub_root=hub_root, raw_config=cfg)
    assert counts.get("repo-stale") is None
    assert counts.get("repo-current") == 1

    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-stale",
        )
        is False
    )
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-current",
        )
        is True
    )


def test_chat_binding_lookup_resolves_repo_from_workspace_paths(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    workspace = _write_manifest_repo(
        hub_root,
        repo_id="repo-chat-managed",
        relative_path="worktrees/chat-app-managed/discord/discord-1",
    )
    workspace_str = str(workspace)

    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-chan-1",
        repo_id=None,
        workspace_path=workspace_str,
    )
    _write_telegram_topic_scope(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        chat_id=100,
        thread_id=1,
        scope=workspace_str,
    )
    _write_telegram_binding(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topic_key=f"100:1:{workspace_str}",
        repo_id=None,
        workspace_path=workspace_str,
    )
    thread_store = PmaThreadStore(hub_root)
    thread_store.create_thread("codex", workspace, repo_id=None)

    counts = active_chat_binding_counts(hub_root=hub_root, raw_config=cfg)
    assert counts.get("repo-chat-managed") == 3
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-chat-managed",
        )
        is True
    )


def test_chat_binding_lookup_resolves_repo_from_custom_manifest_path(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["hub"]["manifest"] = "state/custom-manifest.yml"
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    workspace = _write_manifest_repo(
        hub_root,
        repo_id="repo-custom-manifest",
        relative_path="worktrees/custom/discord/discord-1",
        manifest_relative_path="state/custom-manifest.yml",
    )
    workspace_str = str(workspace)

    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-chan-custom",
        repo_id=None,
        workspace_path=workspace_str,
    )

    counts = active_chat_binding_counts(hub_root=hub_root, raw_config=cfg)
    assert counts.get("repo-custom-manifest") == 1
    assert (
        repo_has_active_chat_binding(
            hub_root=hub_root,
            raw_config=cfg,
            repo_id="repo-custom-manifest",
        )
        is True
    )


def test_chat_binding_queries_prefer_orchestration_owned_bindings(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["discord_bot"]["enabled"] = True
    cfg["telegram_bot"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    workspace = _write_manifest_repo(
        hub_root,
        repo_id="repo-orch",
        relative_path="worktrees/repo-orch",
    )
    _write_orchestration_binding(
        hub_root,
        surface_kind="discord",
        surface_key="channel-1",
        repo_id="repo-orch",
        workspace_path=str(workspace),
    )
    _write_orchestration_binding(
        hub_root,
        surface_kind="telegram",
        surface_key="123:root",
        repo_id="repo-orch",
        workspace_path=str(workspace),
    )

    counts = active_chat_binding_counts_by_source(hub_root=hub_root, raw_config=cfg)

    assert counts["repo-orch"]["discord"] == 1
    assert counts["repo-orch"]["telegram"] == 1
    assert (
        preferred_non_pma_chat_notification_source_for_workspace(
            hub_root=hub_root,
            raw_config=cfg,
            workspace_root=workspace,
        )
        == "telegram"
    )


def test_chat_binding_queries_keep_legacy_counts_for_unmigrated_repos(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["discord_bot"]["enabled"] = True
    cfg["telegram_bot"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    migrated_workspace = _write_manifest_repo(
        hub_root,
        repo_id="repo-migrated",
        relative_path="worktrees/repo-migrated",
    )
    legacy_workspace = _write_manifest_repo(
        hub_root,
        repo_id="repo-legacy",
        relative_path="worktrees/repo-legacy",
    )

    _write_orchestration_binding(
        hub_root,
        surface_kind="discord",
        surface_key="channel-migrated",
        repo_id="repo-migrated",
        workspace_path=str(migrated_workspace),
    )
    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-legacy",
        repo_id="repo-legacy",
        workspace_path=str(legacy_workspace),
    )
    _write_telegram_binding(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topic_key="777:root",
        repo_id="repo-legacy",
        workspace_path=str(legacy_workspace),
    )

    counts = active_chat_binding_counts_by_source(hub_root=hub_root, raw_config=cfg)

    assert counts["repo-migrated"]["discord"] == 1
    assert counts["repo-legacy"]["discord"] == 1
    assert counts["repo-legacy"]["telegram"] == 1


def test_preferred_non_pma_chat_notification_source_uses_freshest_binding(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["discord_bot"]["enabled"] = True
    cfg["telegram_bot"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    workspace = (hub_root / "worktrees" / "repo-a").resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-chan",
        repo_id="repo-a",
        workspace_path=str(workspace),
        updated_at="2026-03-12T02:00:00Z",
    )
    _write_telegram_binding(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topic_key="123:456",
        repo_id="repo-a",
        workspace_path=str(workspace),
        updated_at="2026-03-12T01:00:00Z",
        last_active_at="2026-03-12T01:30:00Z",
    )

    assert (
        preferred_non_pma_chat_notification_source_for_workspace(
            hub_root=hub_root,
            raw_config=cfg,
            workspace_root=workspace,
        )
        == "discord"
    )

    _write_telegram_binding(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topic_key="123:789",
        repo_id="repo-a",
        workspace_path=str(workspace),
        updated_at="2026-03-12T03:00:00Z",
        last_active_at="2026-03-12T03:30:00Z",
    )

    assert (
        preferred_non_pma_chat_notification_source_for_workspace(
            hub_root=hub_root,
            raw_config=cfg,
            workspace_root=workspace,
        )
        == "telegram"
    )


def test_preferred_notification_source_ignores_disabled_surfaces(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["telegram_bot"]["enabled"] = True
    cfg["discord_bot"]["enabled"] = False
    write_test_config(hub_root / CONFIG_FILENAME, cfg)

    workspace = (hub_root / "worktrees" / "repo-b").resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    _write_discord_binding(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        channel_id="discord-stale",
        repo_id="repo-b",
        workspace_path=str(workspace),
        updated_at="2026-03-12T05:00:00Z",
    )
    _write_telegram_binding(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topic_key="300:400",
        repo_id="repo-b",
        workspace_path=str(workspace),
        updated_at="2026-03-12T01:00:00Z",
        last_active_at="2026-03-12T01:30:00Z",
    )

    assert (
        preferred_non_pma_chat_notification_source_for_workspace(
            hub_root=hub_root,
            raw_config=cfg,
            workspace_root=workspace,
        )
        == "telegram"
    )
