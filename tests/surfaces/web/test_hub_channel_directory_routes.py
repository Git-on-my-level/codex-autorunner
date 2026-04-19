import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.support.web_test_helpers import create_test_hub_supervisor
from tests.surfaces.web._hub_test_support import (
    init_git_repo,
    seed_flow_run,
    write_app_server_threads,
    write_discord_binding_rows,
    write_telegram_topic_rows,
    write_usage_rows,
)

from codex_autorunner.core.config import DEFAULT_HUB_CONFIG
from codex_autorunner.core.flows import FlowRunStatus
from codex_autorunner.core.orchestration.bindings import OrchestrationBindingStore
from codex_autorunner.core.orchestration.sqlite import (
    resolve_orchestration_sqlite_path,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.integrations.app_server.threads import (
    FILE_CHAT_PREFIX,
    PMA_KEY,
    PMA_OPENCODE_KEY,
    pma_base_key,
)
from codex_autorunner.integrations.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.integrations.telegram.state import topic_key as telegram_topic_key
from codex_autorunner.server import create_hub_app

pytestmark = [
    pytest.mark.docker_managed_cleanup,
    pytest.mark.slow,
]


def test_hub_channel_directory_route_lists_and_filters(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    create_test_hub_supervisor(hub_root)
    store = ChannelDirectoryStore(hub_root)
    store.record_seen(
        "discord",
        "chan-123",
        "guild-1",
        "CAR HQ / #ops",
        {"guild_id": "guild-1"},
    )
    store.record_seen(
        "telegram",
        "-1001",
        "77",
        "Team Room / Build",
        {"chat_type": "supergroup"},
    )

    client = TestClient(create_hub_app(hub_root))

    listed = client.get("/hub/chat/channels")
    assert listed.status_code == 200
    rows = listed.json()["entries"]
    keys = {row["key"] for row in rows}
    assert "discord:chan-123:guild-1" in keys
    assert "telegram:-1001:77" in keys

    filtered = client.get("/hub/chat/channels", params={"query": "hq", "limit": 10})
    assert filtered.status_code == 200
    filtered_rows = filtered.json()["entries"]
    assert len(filtered_rows) == 1
    assert filtered_rows[0]["key"] == "discord:chan-123:guild-1"

    limited = client.get("/hub/chat/channels", params={"limit": 1})
    assert limited.status_code == 200
    assert len(limited.json()["entries"]) == 1

    bad_limit = client.get("/hub/chat/channels", params={"limit": 0})
    assert bad_limit.status_code == 400
    assert "limit must be greater than 0" in bad_limit.json()["detail"]

    bad_limit_high = client.get("/hub/chat/channels", params={"limit": 1001})
    assert bad_limit_high.status_code == 400
    assert "limit must be <= 1000" in bad_limit_high.json()["detail"]


def test_hub_channel_directory_route_enriches_entries_best_effort(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    cfg = DEFAULT_HUB_CONFIG
    import json

    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg["telegram_bot"]["require_topics"] = True
    supervisor = create_test_hub_supervisor(hub_root, cfg=cfg)
    repo_work = supervisor.create_repo("work")
    repo_final = supervisor.create_repo("final")
    init_git_repo(repo_work.path)
    init_git_repo(repo_final.path)
    (repo_work.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-work", None, "Work / #build", {})
    store.record_seen("discord", "chan-pma", None, "PMA / #ops", {})
    store.record_seen("telegram", "-200", "9", "PMA Topic", {})
    store.record_seen("telegram", "-300", "11", "Final Topic", {})
    store.record_seen("telegram", "-400", "12", "Clean Topic", {})

    write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-work",
                "guild_id": None,
                "workspace_path": str(repo_work.path),
                "repo_id": None,
                "pma_enabled": 0,
                "agent": "codex",
                "updated_at": "2026-01-01T00:00:01Z",
            },
            {
                "channel_id": "chan-pma",
                "guild_id": None,
                "workspace_path": str(repo_work.path),
                "repo_id": "work",
                "pma_enabled": 1,
                "agent": "opencode",
                "updated_at": "2026-01-01T00:00:02Z",
            },
        ],
    )

    scoped_key = telegram_topic_key(-200, 9, scope="dev")
    stale_key = telegram_topic_key(-200, 9, scope="old")
    write_telegram_topic_rows(
        hub_root / ".codex-autorunner" / "telegram_state.sqlite3",
        topics=[
            {
                "topic_key": scoped_key,
                "chat_id": -200,
                "thread_id": 9,
                "scope": "dev",
                "workspace_path": str(repo_work.path),
                "repo_id": None,
                "active_thread_id": "tg-pma-old",
                "payload_json": {"pma_enabled": True, "agent": "codex"},
                "updated_at": "2026-01-01T00:00:03Z",
            },
            {
                "topic_key": stale_key,
                "chat_id": -200,
                "thread_id": 9,
                "scope": "old",
                "workspace_path": str(repo_final.path),
                "repo_id": "final",
                "active_thread_id": "tg-stale",
                "payload_json": {"pma_enabled": False, "agent": "codex"},
                "updated_at": "2026-01-01T00:00:02Z",
            },
            {
                "topic_key": telegram_topic_key(-300, 11),
                "chat_id": -300,
                "thread_id": 11,
                "scope": None,
                "workspace_path": str(repo_final.path),
                "repo_id": None,
                "active_thread_id": "tg-direct-thread",
                "payload_json": {"pma_enabled": False, "agent": "codex"},
                "updated_at": "2026-01-01T00:00:04Z",
            },
            {
                "topic_key": telegram_topic_key(-400, 12),
                "chat_id": -400,
                "thread_id": 12,
                "scope": None,
                "workspace_path": str(repo_final.path),
                "repo_id": "final",
                "active_thread_id": None,
                "payload_json": {"pma_enabled": False, "agent": "codex"},
                "updated_at": "2026-01-01T00:00:05Z",
            },
        ],
        scopes=[
            {
                "chat_id": -200,
                "thread_id": 9,
                "scope": "dev",
                "updated_at": "2026-01-01T00:00:03Z",
            }
        ],
    )

    digest = hashlib.sha256(str(repo_work.path).encode("utf-8")).hexdigest()[:12]
    write_app_server_threads(
        repo_work.path / ".codex-autorunner" / "app_server_threads.json",
        threads={
            f"{FILE_CHAT_PREFIX}discord.chan-work.{digest}": "discord-working-thread",
            PMA_OPENCODE_KEY: "discord-pma-thread",
            PMA_KEY: "wrong-global-pma-thread",
            f"{PMA_KEY}.{telegram_topic_key(-200, 9)}": "telegram-pma-thread",
        },
    )

    pma_store = PmaThreadStore(hub_root)
    discord_pma_thread = pma_store.create_thread(
        "opencode",
        repo_work.path,
        repo_id="work",
    )
    telegram_pma_thread = pma_store.create_thread(
        "codex",
        repo_work.path,
        repo_id="work",
    )
    extra_ticket_flow_thread = pma_store.create_thread(
        "opencode",
        repo_work.path,
        repo_id="work",
        name="ticket-flow:opencode",
        metadata={"thread_kind": "ticket_flow", "run_id": "run-extra"},
    )
    binding_store = OrchestrationBindingStore(hub_root)
    binding_store.upsert_binding(
        surface_kind="discord",
        surface_key="chan-pma",
        thread_target_id=discord_pma_thread["managed_thread_id"],
        agent_id="opencode",
        repo_id="work",
        mode="pma",
        metadata={"channel_id": "chan-pma", "pma_enabled": True},
    )
    binding_store.upsert_binding(
        surface_kind="telegram",
        surface_key=scoped_key,
        thread_target_id=telegram_pma_thread["managed_thread_id"],
        agent_id="codex",
        repo_id="work",
        mode="pma",
        metadata={"topic_key": scoped_key, "pma_enabled": True},
    )

    write_usage_rows(
        repo_work.path / ".codex-autorunner" / "usage" / "opencode_turn_usage.jsonl",
        rows=[
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "session_id": "discord-working-thread",
                "turn_id": "turn-old",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 1,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 1,
                    "total_tokens": 4,
                },
            },
            {
                "timestamp": "2026-01-01T00:00:10Z",
                "session_id": "discord-working-thread",
                "turn_id": "turn-new",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 20,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 40,
                    "total_tokens": 100,
                },
            },
        ],
    )

    seed_flow_run(
        repo_work.path,
        run_id="run-work",
        status=FlowRunStatus.RUNNING,
        diff_events=[
            {"insertions": 2, "deletions": 1, "files_changed": 1},
            {"insertions": 3, "deletions": 2, "files_changed": 2},
        ],
    )
    seed_flow_run(
        repo_final.path,
        run_id="run-final",
        status=FlowRunStatus.COMPLETED,
        diff_events=[{"insertions": 1, "deletions": 0, "files_changed": 1}],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    work = rows["discord:chan-work"]
    assert work["repo_id"] == "work"
    assert work["workspace_path"] == str(repo_work.path)
    assert work["active_thread_id"] == "discord-working-thread"
    assert work["source"] == "discord"
    assert work["provenance"]["source"] == "discord"
    assert work["channel_status"] == "working"
    assert work["status_label"] == "working"
    assert work["diff_stats"] == {"insertions": 5, "deletions": 3, "files_changed": 3}
    assert work["dirty"] is True
    assert work["token_usage"] == {
        "total_tokens": 100,
        "input_tokens": 10,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "reasoning_output_tokens": 40,
        "turn_id": "turn-new",
        "timestamp": "2026-01-01T00:00:10Z",
    }
    assert (
        "display" in work and "seen_at" in work and "meta" in work and "entry" in work
    )

    discord_pma = rows["discord:chan-pma"]
    assert discord_pma["active_thread_id"] == "discord-pma-thread"
    assert discord_pma["source"] == "pma_thread"
    assert discord_pma["provenance"]["source"] == "pma_thread"
    assert discord_pma["provenance"]["agent"] == "opencode"
    assert (
        discord_pma["provenance"]["managed_thread_id"]
        == discord_pma_thread["managed_thread_id"]
    )
    assert (
        discord_pma["provenance"]["managed_thread_id"]
        != discord_pma["active_thread_id"]
    )
    assert f"pma_thread:{discord_pma_thread['managed_thread_id']}" not in rows

    telegram_pma = rows["telegram:-200:9"]
    assert telegram_pma["active_thread_id"] == "telegram-pma-thread"
    assert telegram_pma["source"] == "pma_thread"
    assert telegram_pma["provenance"]["source"] == "pma_thread"
    assert telegram_pma["provenance"]["agent"] == "codex"
    assert (
        telegram_pma["provenance"]["managed_thread_id"]
        == telegram_pma_thread["managed_thread_id"]
    )
    assert (
        telegram_pma["provenance"]["managed_thread_id"]
        != telegram_pma["active_thread_id"]
    )
    assert f"pma_thread:{telegram_pma_thread['managed_thread_id']}" not in rows

    extra_pma_key = f"pma_thread:{extra_ticket_flow_thread['managed_thread_id']}"
    assert extra_pma_key in rows
    extra_pma = rows[extra_pma_key]
    assert extra_pma["display"] == "ticket-flow:opencode"
    assert extra_pma["provenance"]["thread_kind"] == "ticket_flow"
    assert extra_pma["provenance"]["run_id"] == "run-extra"

    telegram_final = rows["telegram:-300:11"]
    assert telegram_final["active_thread_id"] == "tg-direct-thread"
    assert telegram_final["source"] == "telegram"
    assert telegram_final["provenance"]["source"] == "telegram"
    assert telegram_final["channel_status"] == "final"
    assert telegram_final["status_label"] == "final"
    assert telegram_final["dirty"] is False

    telegram_clean = rows["telegram:-400:12"]
    assert telegram_clean["source"] == "telegram"
    assert telegram_clean["provenance"]["source"] == "telegram"
    assert telegram_clean["channel_status"] == "clean"
    assert telegram_clean["status_label"] == "clean"


def test_hub_channel_directory_route_uses_managed_thread_id_for_pma_usage(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("work")

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-pma", None, "PMA / #ops", {})
    write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-pma",
                "guild_id": None,
                "workspace_path": str(repo.path),
                "repo_id": "work",
                "pma_enabled": 1,
                "agent": "opencode",
                "updated_at": "2026-01-01T00:00:02Z",
            }
        ],
    )

    pma_store = PmaThreadStore(hub_root)
    created = pma_store.create_thread(
        "opencode",
        repo.path,
        repo_id="work",
        name="discord:chan-pma",
    )
    managed_thread_id = str(created["managed_thread_id"])
    legacy_backend_thread_id = "legacy-backend-thread"

    orch_db_path = resolve_orchestration_sqlite_path(hub_root)
    conn = sqlite3.connect(orch_db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET backend_thread_id = ?
                 WHERE thread_target_id = ?
                """,
                (legacy_backend_thread_id, managed_thread_id),
            )
    finally:
        conn.close()

    write_usage_rows(
        repo.path / ".codex-autorunner" / "usage" / "opencode_turn_usage.jsonl",
        rows=[
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "session_id": managed_thread_id,
                "turn_id": "managed-turn",
                "usage": {
                    "input_tokens": 2,
                    "cached_input_tokens": 3,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 7,
                    "total_tokens": 17,
                },
            },
            {
                "timestamp": "2026-01-01T00:00:10Z",
                "session_id": legacy_backend_thread_id,
                "turn_id": "legacy-turn",
                "usage": {
                    "input_tokens": 11,
                    "cached_input_tokens": 13,
                    "output_tokens": 17,
                    "reasoning_output_tokens": 19,
                    "total_tokens": 60,
                },
            },
        ],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    standalone_key = f"pma_thread:{managed_thread_id}"
    assert standalone_key in rows
    assert rows[standalone_key]["token_usage"] == {
        "total_tokens": 17,
        "input_tokens": 2,
        "cached_input_tokens": 3,
        "output_tokens": 5,
        "reasoning_output_tokens": 7,
        "turn_id": "managed-turn",
        "timestamp": "2026-01-01T00:00:00Z",
    }


def test_hub_channel_directory_route_surfaces_agent_workspace_binding_metadata(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    workspace = supervisor.create_agent_workspace(
        workspace_id="zc-main",
        runtime="zeroclaw",
        display_name="ZeroClaw Main",
    )

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-zc", None, "ZeroClaw / #main", {})

    write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-zc",
                "guild_id": None,
                "workspace_path": str(workspace.path.resolve()),
                "repo_id": None,
                "resource_kind": "agent_workspace",
                "resource_id": workspace.id,
                "pma_enabled": 0,
                "agent": "codex",
                "updated_at": "2026-01-01T00:00:01Z",
            }
        ],
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200

    row = next(
        entry
        for entry in response.json()["entries"]
        if entry["key"] == "discord:chan-zc"
    )
    assert row["workspace_path"] == str(workspace.path.resolve())
    assert row.get("repo_id") is None
    assert row.get("resource_kind") == "agent_workspace"
    assert row.get("resource_id") == workspace.id
    assert row["provenance"]["resource_kind"] == "agent_workspace"
    assert row["provenance"]["resource_id"] == workspace.id


def test_hub_channel_directory_route_includes_pma_managed_threads(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    base = supervisor.create_repo("base")
    init_git_repo(base.path)
    worktree = supervisor.create_worktree(
        base_repo_id="base",
        branch="feature/pma-provenance",
        start_point="HEAD",
    )

    store = PmaThreadStore(hub_root)
    created = store.create_thread(
        "codex",
        worktree.path,
        repo_id=worktree.id,
        name="ticket-flow:codex",
        metadata={
            "thread_kind": "ticket_flow",
            "flow_type": "ticket_flow",
            "run_id": "run-123",
        },
    )
    managed_thread_id = str(created["managed_thread_id"])

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    pma_key = f"pma_thread:{managed_thread_id}"
    assert pma_key in rows
    pma_row = rows[pma_key]
    assert pma_row["repo_id"] == worktree.id
    assert pma_row["workspace_path"] == str(worktree.path)
    assert pma_row["source"] == "pma_thread"
    assert pma_row["provenance"]["source"] == "pma_thread"
    assert pma_row["provenance"]["agent"] == "codex"
    assert pma_row["provenance"]["managed_thread_id"] == managed_thread_id
    assert pma_row["provenance"]["thread_kind"] == "ticket_flow"
    assert pma_row["provenance"]["run_id"] == "run-123"
    assert pma_row["channel_status"] == "idle"
    assert pma_row["status_label"] == "idle"
    assert pma_row["provenance"]["status_reason_code"] == "thread_created"

    filtered = client.get("/hub/chat/channels", params={"query": "pma", "limit": 1})
    assert filtered.status_code == 200
    filtered_rows = filtered.json()["entries"]
    assert len(filtered_rows) == 1
    assert filtered_rows[0]["key"] == pma_key


def test_hub_channel_directory_route_ignores_repo_mode_binding_for_pma_rows(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("work")

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-pma", None, "PMA / #ops", {})

    write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-pma",
                "guild_id": None,
                "workspace_path": str(repo.path),
                "repo_id": "work",
                "pma_enabled": 1,
                "agent": "opencode",
                "updated_at": "2026-01-01T00:00:02Z",
            }
        ],
    )
    write_app_server_threads(
        repo.path / ".codex-autorunner" / "app_server_threads.json",
        threads={PMA_OPENCODE_KEY: "discord-pma-thread"},
    )

    pma_store = PmaThreadStore(hub_root)
    stale_repo_thread = pma_store.create_thread(
        "opencode",
        repo.path,
        repo_id="work",
        name="repo-thread",
    )
    live_pma_thread = pma_store.create_thread(
        "opencode",
        repo.path,
        repo_id="work",
        name="discord:chan-pma",
    )
    db_path = resolve_orchestration_sqlite_path(hub_root)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET updated_at = ?
                 WHERE thread_target_id = ?
                """,
                ("2026-01-01T00:00:01Z", stale_repo_thread["managed_thread_id"]),
            )
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET updated_at = ?
                 WHERE thread_target_id = ?
                """,
                ("2026-01-01T00:00:05Z", live_pma_thread["managed_thread_id"]),
            )
    finally:
        conn.close()
    OrchestrationBindingStore(hub_root).upsert_binding(
        surface_kind="discord",
        surface_key="chan-pma",
        thread_target_id=stale_repo_thread["managed_thread_id"],
        agent_id="opencode",
        repo_id="work",
        mode="repo",
        metadata={"channel_id": "chan-pma", "pma_enabled": False},
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    channel_row = rows["discord:chan-pma"]
    assert channel_row["active_thread_id"] == "discord-pma-thread"
    assert channel_row["source"] == "pma_thread"
    assert (
        channel_row["provenance"]["managed_thread_id"]
        == live_pma_thread["managed_thread_id"]
    )
    assert (
        channel_row["provenance"]["managed_thread_id"]
        != stale_repo_thread["managed_thread_id"]
    )


def test_hub_channel_directory_route_keeps_standalone_pending_pma_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("work")

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-pma-pending", None, "PMA / #pending", {})

    write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-pma-pending",
                "guild_id": None,
                "workspace_path": str(repo.path),
                "repo_id": "work",
                "pma_enabled": 1,
                "agent": "opencode",
                "updated_at": "2026-01-01T00:00:02Z",
            }
        ],
    )

    pma_thread = PmaThreadStore(hub_root).create_thread(
        "opencode",
        repo.path,
        repo_id="work",
        name="discord:chan-pma-pending",
    )
    OrchestrationBindingStore(hub_root).upsert_binding(
        surface_kind="discord",
        surface_key="chan-pma-pending",
        thread_target_id=pma_thread["managed_thread_id"],
        agent_id="opencode",
        repo_id="work",
        mode="pma",
        metadata={"channel_id": "chan-pma-pending", "pma_enabled": True},
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    channel_row = rows["discord:chan-pma-pending"]
    assert channel_row["source"] == "pma_thread"
    assert channel_row.get("active_thread_id") is None
    assert channel_row["channel_status"] == "clean"
    assert (
        channel_row["provenance"]["managed_thread_id"]
        == pma_thread["managed_thread_id"]
    )

    standalone_key = f"pma_thread:{pma_thread['managed_thread_id']}"
    assert standalone_key in rows
    standalone_row = rows[standalone_key]
    assert standalone_row["source"] == "pma_thread"
    assert standalone_row["active_thread_id"] == pma_thread["managed_thread_id"]


def test_hub_channel_directory_route_uses_profiled_pma_registry_key(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("work")

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-hermes", None, "PMA / #hermes", {})

    write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-hermes",
                "guild_id": None,
                "workspace_path": str(repo.path),
                "repo_id": "work",
                "pma_enabled": 1,
                "agent": "hermes",
                "agent_profile": "m4-pma",
                "updated_at": "2026-01-01T00:00:02Z",
            }
        ],
    )
    write_app_server_threads(
        repo.path / ".codex-autorunner" / "app_server_threads.json",
        threads={
            pma_base_key("hermes", "m4-pma"): "discord-hermes-pma-thread",
        },
    )

    pma_thread = PmaThreadStore(hub_root).create_thread(
        "hermes",
        repo.path,
        repo_id="work",
        name="discord:chan-hermes",
        metadata={"agent_profile": "m4-pma"},
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    channel_row = rows["discord:chan-hermes"]
    assert channel_row["source"] == "pma_thread"
    assert channel_row["active_thread_id"] == "discord-hermes-pma-thread"
    assert channel_row["provenance"]["agent"] == "hermes"
    assert (
        channel_row["provenance"]["managed_thread_id"]
        == pma_thread["managed_thread_id"]
    )

    standalone_key = f"pma_thread:{pma_thread['managed_thread_id']}"
    assert standalone_key not in rows


def test_hub_channel_directory_route_falls_back_unknown_agents_to_codex_pma_key(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    supervisor = create_test_hub_supervisor(hub_root)
    repo = supervisor.create_repo("work")

    store = ChannelDirectoryStore(hub_root)
    store.record_seen("discord", "chan-stale-agent", None, "PMA / #stale-agent", {})

    write_discord_binding_rows(
        hub_root / ".codex-autorunner" / "discord_state.sqlite3",
        rows=[
            {
                "channel_id": "chan-stale-agent",
                "guild_id": None,
                "workspace_path": str(repo.path),
                "repo_id": "work",
                "pma_enabled": 1,
                "agent": "retired-agent",
                "updated_at": "2026-01-01T00:00:02Z",
            }
        ],
    )
    write_app_server_threads(
        repo.path / ".codex-autorunner" / "app_server_threads.json",
        threads={pma_base_key("codex", None): "discord-codex-pma-thread"},
    )

    pma_thread = PmaThreadStore(hub_root).create_thread(
        "codex",
        repo.path,
        repo_id="work",
        name="discord:chan-stale-agent",
    )

    client = TestClient(create_hub_app(hub_root))
    response = client.get("/hub/chat/channels")
    assert response.status_code == 200
    rows = {entry["key"]: entry for entry in response.json()["entries"]}

    channel_row = rows["discord:chan-stale-agent"]
    assert channel_row["source"] == "pma_thread"
    assert channel_row["active_thread_id"] == "discord-codex-pma-thread"
    assert channel_row["provenance"]["agent"] == "codex"
    assert (
        channel_row["provenance"]["managed_thread_id"]
        == pma_thread["managed_thread_id"]
    )

    standalone_key = f"pma_thread:{pma_thread['managed_thread_id']}"
    assert standalone_key not in rows
