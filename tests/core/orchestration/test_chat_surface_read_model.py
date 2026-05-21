from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.adapters.chat.channel_directory import ChannelDirectoryStore
from codex_autorunner.core.domain.workspace_scope import (
    workspace_scope_index_from_snapshots,
)
from codex_autorunner.core.flows import FlowRunStatus, FlowStore
from codex_autorunner.core.orchestration import (
    ChatSurfaceReadService,
    OrchestrationBindingStore,
    SQLiteChatSurfaceEventJournal,
    ticket_flow_thread_metadata,
)
from codex_autorunner.core.orchestration.chat_surface_read_model import (
    _chat_index_sort_key_parts,
    canonical_owner_fields,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "chat_surface"
    / "lifecycle_contract.json"
)


def test_canonical_owner_fields_preserves_unindexed_review_resource_repo_id(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "review-checkout"
    checkout.mkdir()
    index = workspace_scope_index_from_snapshots([])

    fields = canonical_owner_fields(
        index,
        repo_id="repo-from-review-diff",
        resource_kind="file",
        resource_id="src/example.py",
        workspace_root=str(checkout),
    )

    assert fields == {
        "repo_id": "repo-from-review-diff",
        "worktree_id": None,
        "resource_kind": "file",
        "resource_id": "src/example.py",
        "workspace_root": str(checkout.resolve()),
        "scope_urn": "repo:repo-from-review-diff",
    }


def _seed_thread(
    hub_root: Path,
    *,
    thread_id: str,
    repo_id: str = "repo-1",
    resource_kind: str = "repo",
    resource_id: str | None = None,
    lifecycle_status: str = "active",
    runtime_status: str = "idle",
    display_name: str | None = None,
    last_message_preview: str | None = None,
    metadata: dict[str, object] | None = None,
    updated_at: str = "2026-05-11T00:00:10Z",
) -> None:
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id,
                agent_id,
                repo_id,
                resource_kind,
                resource_id,
                display_name,
                lifecycle_status,
                runtime_status,
                last_message_preview,
                metadata_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                "codex",
                repo_id,
                resource_kind,
                resource_id or repo_id,
                display_name or f"Thread {thread_id}",
                lifecycle_status,
                runtime_status,
                last_message_preview,
                json.dumps(metadata or {}),
                "2026-05-11T00:00:00Z",
                updated_at,
            ),
        )


def _seed_execution(
    hub_root: Path,
    *,
    thread_id: str,
    status: str,
    execution_id: str | None = None,
    prompt_text: str | None = None,
    metadata: dict[str, object] | None = None,
    created_at: str = "2026-05-11T00:01:00Z",
) -> None:
    eid = execution_id or f"exec-{thread_id}-{status}"
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_executions (
                execution_id,
                thread_target_id,
                request_kind,
                prompt_text,
                metadata_json,
                status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eid,
                thread_id,
                "message",
                prompt_text,
                json.dumps(metadata or {}),
                status,
                created_at,
            ),
        )


def _write_ticket(path: Path, *, done: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f'title: "{path.stem}"',
                'agent: "codex"',
                f"done: {'true' if done else 'false'}",
                f'ticket_id: "tkt_{path.stem}"',
                "---",
                "",
                "## Goal",
                "- Test ticket.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _seed_ticket_flow_projection(
    hub_root: Path,
    *,
    run_id: str,
    status: str,
    repo_id: str = "repo-1",
    workspace_root: Path | None = None,
    current_ticket: str | None = None,
    current_ticket_done: bool | None = None,
) -> None:
    ticket_engine: dict[str, object] = {"status": status}
    if current_ticket is not None:
        ticket_engine["current_ticket"] = current_ticket
    if current_ticket_done is not None:
        ticket_engine["commit"] = {
            "pending": True,
            "current_ticket_done": current_ticket_done,
        }
    summary = {
        "workspace_root": str((workspace_root or hub_root.parent / "repo").resolve()),
        "current_ticket": current_ticket,
        "ticket_engine": ticket_engine,
        "projection_source": "repo_flow_store",
    }
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            """
            INSERT INTO orch_flow_run_projections (
                flow_run_id,
                repo_id,
                flow_type,
                status,
                summary_json,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                repo_id,
                "ticket_flow",
                status,
                json.dumps(summary),
                "2026-05-11T00:00:20Z",
            ),
        )


def _seed_repo_flow_store_ticket_run(
    repo_root: Path,
    *,
    run_id: str,
    status: FlowRunStatus,
    current_ticket: str,
    current_ticket_done: bool,
) -> None:
    db_path = FlowStore.default_path(repo_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={"workspace_root": str(repo_root)},
            metadata={"repo_id": "repo-1"},
            state={
                "ticket_engine": {
                    "status": status.value,
                    "current_ticket": current_ticket,
                    "commit": {
                        "pending": True,
                        "current_ticket_done": current_ticket_done,
                    },
                }
            },
        )
        store.update_flow_run_status(run_id, status)


def _mark_projected_thread_archived(hub_root: Path, thread_id: str) -> None:
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        row = conn.execute(
            """
            SELECT row_json
              FROM orch_chat_index_projection
             WHERE managed_thread_id = ?
            """,
            (thread_id,),
        ).fetchone()
        assert row is not None
        row_json = json.loads(row["row_json"])
        row_json["lifecycle_status"] = "archived"
        row_json["archive_state"] = "archived"
        conn.execute(
            """
            UPDATE orch_chat_index_projection
               SET lifecycle_status = 'archived',
                   effective_status = 'archived',
                   row_json = ?
             WHERE managed_thread_id = ?
            """,
            (json.dumps(row_json, sort_keys=True, separators=(",", ":")), thread_id),
        )


def test_chat_surface_read_model_projects_contract_fixture_surface_inventory(
    tmp_path: Path,
) -> None:
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    expected_surface_kinds = {
        scenario["surface_kind"] for scenario in fixture["scenarios"]
    }
    hub_root = tmp_path / "hub"

    _seed_thread(hub_root, thread_id="thread-pma")
    _seed_thread(hub_root, thread_id="thread-discord")
    _seed_thread(hub_root, thread_id="thread-telegram")
    _seed_execution(hub_root, thread_id="thread-telegram", status="running")
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-discord",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Discord channel"},
    )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="telegram",
        surface_key="-1001:77",
        thread_target_id="thread-telegram",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Telegram topic"},
    )
    SQLiteChatSurfaceEventJournal(hub_root, durable=False).append_event(
        idempotency_key="web:queued",
        event_type="queue.state_changed",
        surface_kind="web",
        surface_key="chat:repo-1:thread-web",
        managed_thread_id="thread-web",
        repo_id="repo-1",
        status="queued",
    )
    ChannelDirectoryStore(hub_root).record_seen(
        "discord",
        "guild-channel-discovered",
        None,
        "Discovered channel",
    )
    PmaNotificationStore(hub_root).record_notification(
        correlation_id="corr-1",
        source_kind="pma",
        delivery_mode="direct",
        surface_kind="discord",
        surface_key="guild:channel",
        delivery_record_id="delivery-1",
        repo_id="repo-1",
        managed_thread_id="thread-discord",
        notification_id="notif-1",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot()
    surfaces = snapshot["surfaces"]
    observed_kinds = {surface["surface_kind"] for surface in surfaces}

    assert expected_surface_kinds <= observed_kinds
    by_kind_key = {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in surfaces
    }
    assert by_kind_key[("pma", "thread-pma")]["managed_thread_id"] == "thread-pma"
    assert by_kind_key[("discord", "guild:channel")]["lifecycle"] == "bound"
    assert by_kind_key[("telegram", "-1001:77")]["lifecycle"] == "running"
    assert by_kind_key[("web", "chat:repo-1:thread-web")]["lifecycle"] == "queued"
    assert (
        by_kind_key[("notification", "notification:notif-1")]["managed_thread_id"]
        == "thread-discord"
    )
    assert (
        "channel_directory"
        in by_kind_key[("discord", "guild-channel-discovered")]["facts"]
    )


def test_chat_index_omits_stale_bound_surfaces_without_managed_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="guild:stale-thread",
        thread_target_id="missing-thread",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Stale Discord thread"},
    )
    ChannelDirectoryStore(hub_root).record_seen(
        "discord",
        "guild-discovered-only",
        None,
        "Discovered only",
    )
    _seed_thread(hub_root, thread_id="live-thread")

    service = ChatSurfaceReadService(hub_root, durable=False)
    snapshot = service.snapshot()
    surface_ids = {
        surface["managed_thread_id"]
        for surface in snapshot["surfaces"]
        if surface.get("managed_thread_id")
    }
    assert {"missing-thread", "live-thread"} <= surface_ids

    index = service.chat_index_snapshot(view="all", limit=20)
    assert [row["managed_thread_id"] for row in index["rows"]] == ["live-thread"]


def test_chat_index_uses_managed_lifecycle_after_discord_surface_rebind(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-a",
        lifecycle_status="archived",
        runtime_status="completed",
    )
    _seed_thread(
        hub_root,
        thread_id="thread-b",
        lifecycle_status="active",
        runtime_status="running",
    )
    bindings = OrchestrationBindingStore(hub_root, durable=False)
    first = bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-a",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Discord channel"},
    )
    bindings.disable_binding(binding_id=first.binding_id)
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-b",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Discord channel"},
    )

    service = ChatSurfaceReadService(hub_root, durable=False)

    discord = service.chat_index_snapshot(
        view="all",
        surface_kind="discord",
        limit=20,
    )
    assert [row["managed_thread_id"] for row in discord["rows"]] == ["thread-b"]
    assert discord["rows"][0]["archive_state"] == "active"
    assert discord["rows"][0]["lifecycle"] == "running"
    assert discord["counters"]["total"] == 1
    assert discord["counters"]["archived"] == 0

    archived = service.chat_index_snapshot(view="archived", limit=20)
    assert [row["managed_thread_id"] for row in archived["rows"]] == ["thread-a"]
    assert archived["counters"]["total"] == 1


def test_chat_index_uses_managed_lifecycle_after_telegram_surface_rebind(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="telegram-thread-a",
        lifecycle_status="archived",
        runtime_status="completed",
    )
    _seed_thread(
        hub_root,
        thread_id="telegram-thread-b",
        lifecycle_status="active",
        runtime_status="running",
    )
    bindings = OrchestrationBindingStore(hub_root, durable=False)
    first = bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="-1001:77",
        thread_target_id="telegram-thread-a",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Telegram topic"},
    )
    bindings.disable_binding(binding_id=first.binding_id)
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="-1001:77",
        thread_target_id="telegram-thread-b",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Telegram topic"},
    )

    service = ChatSurfaceReadService(hub_root, durable=False)

    telegram = service.chat_index_snapshot(
        view="all",
        surface_kind="telegram",
        limit=20,
    )
    assert [row["managed_thread_id"] for row in telegram["rows"]] == [
        "telegram-thread-b"
    ]
    assert telegram["rows"][0]["archive_state"] == "active"
    assert telegram["counters"]["total"] == 1
    assert telegram["counters"]["archived"] == 0

    archived = service.chat_index_snapshot(view="archived", limit=20)
    assert [row["managed_thread_id"] for row in archived["rows"]] == [
        "telegram-thread-a"
    ]


def test_chat_index_projects_backend_facets_counts_and_filters(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    for index in range(60):
        _seed_thread(
            hub_root,
            thread_id=f"regular-{index:02d}",
            updated_at=f"2026-05-11T01:{index % 60:02d}:00Z",
        )
    _seed_thread(
        hub_root,
        thread_id="zzz-automation-old",
        metadata={"automation_job_id": "job-1", "automation_rule_id": "rule-1"},
        updated_at="2026-05-10T00:00:00Z",
    )

    service = ChatSurfaceReadService(hub_root, durable=False)
    first_page = service.chat_index_snapshot(view="all", limit=10)
    assert "zzz-automation-old" not in {
        row["managed_thread_id"] for row in first_page["rows"]
    }
    assert first_page["facet_counts"]["category"]["automation"] == 1

    automation = service.chat_index_snapshot(
        view="all",
        facets={"categories": ["automation"]},
        limit=10,
    )
    assert [row["managed_thread_id"] for row in automation["rows"]] == [
        "zzz-automation-old"
    ]
    assert automation["rows"][0]["facets"]["category"] == "automation"
    assert "automation" in automation["rows"][0]["facets"]["turn_kinds"]
    assert "automation" in automation["rows"][0]["facets"]["origin_kinds"]


def test_chat_index_rebuilds_projection_when_facet_schema_marker_is_missing(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="automation-upgrade",
        metadata={"automation_job_id": "job-1", "automation_rule_id": "rule-1"},
    )
    service = ChatSurfaceReadService(hub_root, durable=False)
    service.rebuild_chat_index_projection()

    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute("""
            UPDATE orch_chat_index_projection
               SET facet_category = NULL,
                   facet_turn_kind_list = '',
                   facet_origin_kind_list = '',
                   facet_transport_list = '',
                   facet_scope_kind = NULL,
                   facet_scope_id = NULL,
                   facet_agent_kind = NULL
            """)
        conn.execute("""
            DELETE FROM orch_chat_index_projection_meta
             WHERE key = 'projection_schema_version'
            """)

    assert service.chat_index_projection_status()["needs_rebuild"] is True
    automation = service.chat_index_snapshot(
        view="all",
        facets={"categories": ["automation"]},
        limit=10,
    )

    assert [row["managed_thread_id"] for row in automation["rows"]] == [
        "automation-upgrade"
    ]
    assert automation["rows"][0]["facets"]["category"] == "automation"
    assert service.chat_index_projection_status()["needs_rebuild"] is False


def test_chat_index_regular_category_excludes_ticket_automation_and_system(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(hub_root, thread_id="regular")
    _seed_thread(
        hub_root,
        thread_id="ticket",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-1",
            ticket_id="TICKET-001",
            workspace_root=str(tmp_path / "repo"),
        ),
    )
    _seed_thread(
        hub_root,
        thread_id="automation",
        metadata={"automation_job_id": "job-1"},
    )
    _seed_thread(hub_root, thread_id="system")
    _seed_execution(
        hub_root,
        thread_id="system",
        status="completed",
        metadata={},
    )
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            """
            UPDATE orch_thread_executions
               SET request_kind = 'recovery',
                   turn_request_json = ?
             WHERE thread_target_id = 'system'
            """,
            (
                json.dumps(
                    {
                        "request_id": "req-system",
                        "target_kind": "thread",
                        "target_id": "system",
                        "request_kind": "recovery",
                        "prompt": "recover",
                        "origin": {"kind": "system", "source_id": "test"},
                    }
                ),
            ),
        )

    service = ChatSurfaceReadService(hub_root, durable=False)
    regular = service.chat_index_snapshot(
        view="all",
        facets={"categories": ["regular"]},
        limit=20,
    )
    assert [row["managed_thread_id"] for row in regular["rows"]] == ["regular"]

    all_rows = {
        row["managed_thread_id"]: row
        for row in service.chat_index_snapshot(view="all", limit=20)["rows"]
    }
    assert all_rows["ticket"]["facets"]["category"] == "ticket_run"
    assert all_rows["automation"]["facets"]["category"] == "automation"
    assert all_rows["system"]["facets"]["category"] == "system"


def test_chat_index_transport_facets_and_counts(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(hub_root, thread_id="pma-thread")
    _seed_thread(hub_root, thread_id="discord-thread")
    _seed_thread(hub_root, thread_id="telegram-thread")
    bindings = OrchestrationBindingStore(hub_root, durable=False)
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="discord-thread",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
    )
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="-1001:77",
        thread_target_id="telegram-thread",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
    )
    PmaNotificationStore(hub_root).record_notification(
        correlation_id="notif-corr",
        source_kind="pma",
        delivery_mode="direct",
        surface_kind="discord",
        surface_key="guild:channel",
        delivery_record_id="delivery-1",
        repo_id="repo-1",
        managed_thread_id="discord-thread",
        notification_id="notif-1",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="all",
        limit=20,
    )

    assert snapshot["facet_counts"]["transport"]["pma"] == 3
    assert snapshot["facet_counts"]["transport"]["discord"] == 1
    assert snapshot["facet_counts"]["transport"]["telegram"] == 1
    assert snapshot["facet_counts"]["transport"]["notification"] == 1
    rows = {row["managed_thread_id"]: row for row in snapshot["rows"]}
    assert "notification" in rows["discord-thread"]["facets"]["transports"]


def test_chat_index_visible_chrome_uses_user_visible_prompt_metadata_not_delimiter_stripping(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    injected_prompt = (
        "<injected context>\n"
        "model-only deployment instructions\n"
        "</injected context>\n\n"
        "Investigate checkout failure"
    )
    _seed_thread(
        hub_root,
        thread_id="thread-injected",
        display_name=injected_prompt,
        last_message_preview=injected_prompt,
    )
    _seed_execution(
        hub_root,
        thread_id="thread-injected",
        status="completed",
        prompt_text=injected_prompt,
        metadata={
            "raw_model_prompt": injected_prompt,
            "user_visible_text": "Investigate checkout failure",
            "title_seed": "Investigate checkout failure",
            "capsule_refs": [
                {
                    "capsule_id": "car.repo_awareness",
                    "capsule_version": "1",
                    "visibility": "model_only",
                    "scope": "thread",
                    "source_digest": "digest-1",
                }
            ],
        },
    )

    service = ChatSurfaceReadService(hub_root, durable=False)
    row = service.chat_index_snapshot(view="all", limit=20)["rows"][0]

    assert row["display_title"] == "Investigate checkout failure"
    assert row["last_message_preview"] == "Investigate checkout failure"
    assert "model-only" not in row["search_text"]
    assert "<injected context>" not in json.dumps(row["surfaces"])
    assert "<injected context>" not in json.dumps(row["primary_surface"])

    search = service.chat_index_snapshot(query="checkout failure", limit=20)
    assert [item["managed_thread_id"] for item in search["rows"]] == ["thread-injected"]
    leaked = service.chat_index_snapshot(query="model-only", limit=20)
    assert leaked["rows"] == []


def test_chat_index_falls_back_to_user_visible_execution_for_attachment_only_turn(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-attachment",
        display_name="Thread thread-attachment",
        last_message_preview="<injected context>runtime attachment notes</injected context>",
    )
    _seed_execution(
        hub_root,
        thread_id="thread-attachment",
        status="completed",
        prompt_text="<injected context>runtime attachment notes</injected context>",
        metadata={
            "raw_model_prompt": "<injected context>runtime attachment notes</injected context>",
            "user_visible_text": "Attachment: crash.log",
            "title_seed": "Attachment: crash.log",
        },
    )

    row = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="all",
        limit=20,
    )["rows"][0]

    assert row["display_title"] == "Attachment: crash.log"
    assert row["last_message_preview"] == "Attachment: crash.log"
    assert "runtime attachment notes" not in row["search_text"]


def test_chat_index_does_not_promote_compact_seed_to_visible_chrome(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    compact_prompt = (
        "Context from previous conversation:\n\n"
        "secret implementation notes\n\n"
        "Continue from this context. Ask for missing info if needed.\n\n"
        "Continue the release cleanup"
    )
    _seed_thread(
        hub_root,
        thread_id="thread-compact",
        display_name=compact_prompt,
        last_message_preview=compact_prompt,
    )
    _seed_execution(
        hub_root,
        thread_id="thread-compact",
        status="completed",
        prompt_text="Continue the release cleanup",
    )

    service = ChatSurfaceReadService(hub_root, durable=False)
    row = service.chat_index_snapshot(view="all", limit=20)["rows"][0]

    assert row["display_title"] == "Continue the release cleanup"
    assert row["last_message_preview"] == "Continue the release cleanup"
    assert "secret implementation notes" not in row["search_text"]
    assert (
        service.chat_index_snapshot(query="secret implementation", limit=20)["rows"]
        == []
    )


def test_chat_index_sort_key_parts_serializes_missing_activity_as_null() -> None:
    parts = _chat_index_sort_key_parts(
        {"managed_thread_id": "thread-1", "unread_count": 0}
    )

    assert parts["last_activity_desc"] is None


def test_chat_index_canonicalizes_legacy_worktree_repo_id_from_hub_topology(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    worktree_root = hub_root / "worktrees" / "repo--discord-1"
    worktree_root.mkdir(parents=True)
    state_dir = hub_root / ".codex-autorunner"
    state_dir.mkdir()
    (state_dir / "hub_state.json").write_text(
        json.dumps(
            {
                "last_scan_at": "2026-05-11T00:00:00Z",
                "repos": [
                    {
                        "id": "repo",
                        "path": "repos/repo",
                        "kind": "base",
                        "status": "uninitialized",
                    },
                    {
                        "id": "repo--discord-1",
                        "path": "worktrees/repo--discord-1",
                        "kind": "worktree",
                        "worktree_of": "repo",
                        "status": "uninitialized",
                    },
                ],
                "pinned_parent_repo_ids": [],
            }
        ),
        encoding="utf-8",
    )
    _seed_thread(
        hub_root,
        thread_id="discord-thread",
        repo_id="repo--discord-1",
        resource_kind="repo",
        resource_id="repo--discord-1",
    )

    index = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="all",
        limit=20,
    )

    assert index["rows"][0]["repo_id"] == "repo"
    assert index["rows"][0]["worktree_id"] == "repo--discord-1"
    assert index["rows"][0]["resource_kind"] == "worktree"
    assert index["rows"][0]["resource_id"] == "repo--discord-1"


def test_chat_surface_read_model_orders_and_limits_snapshot(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=False)
    journal.append_event(
        idempotency_key="b",
        event_type="channel_directory.discovered",
        surface_kind="telegram",
        surface_key="2",
        status="discovered",
    )
    journal.append_event(
        idempotency_key="a",
        event_type="channel_directory.discovered",
        surface_kind="discord",
        surface_key="1",
        status="discovered",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot(limit=1)

    assert [surface["surface_urn"] for surface in snapshot["surfaces"]] == ["discord:1"]
    assert snapshot["limits"] == {"requested": 1, "returned": 1, "max": 1000}
    assert snapshot["cursor"] == 2


def test_chat_surface_read_model_projects_thread_identity_from_metadata_json(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-pma-profiled",
        metadata={"agent_profile": "m4-pma", "model": "gpt-5.5"},
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot()
    by_kind_key = {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in snapshot["surfaces"]
    }

    surface = by_kind_key[("pma", "thread-pma-profiled")]
    assert surface["managed_thread_id"] == "thread-pma-profiled"
    assert surface["metadata"]["agent_id"] == "codex"
    assert surface["metadata"]["agent_profile"] == "m4-pma"
    assert surface["metadata"]["model"] == "gpt-5.5"


def test_chat_surface_read_model_ignores_running_execution_on_archived_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-archived",
        lifecycle_status="archived",
        runtime_status="archived",
    )
    _seed_execution(hub_root, thread_id="thread-archived", status="running")

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot()
    by_kind_key = {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in snapshot["surfaces"]
    }

    surface = by_kind_key[("pma", "thread-archived")]
    assert surface["lifecycle"] == "archived"
    assert surface["metadata"]["runtime_status"] == "archived"
    assert surface["metadata"]["latest_execution_status"] is None
    assert surface["metadata"]["active_turn_id"] is None


def test_chat_surface_read_model_keeps_queued_execution_when_runtime_completed(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    thread_id = "thread-queued-followup"
    _seed_thread(
        hub_root,
        thread_id=thread_id,
        runtime_status="completed",
    )
    _seed_execution(
        hub_root,
        thread_id=thread_id,
        status="completed",
        execution_id=f"{thread_id}-turn-a",
        created_at="2026-05-11T00:01:00Z",
    )
    _seed_execution(
        hub_root,
        thread_id=thread_id,
        status="queued",
        execution_id=f"{thread_id}-turn-b",
        created_at="2026-05-11T00:02:00Z",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot()
    by_kind_key = {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in snapshot["surfaces"]
    }

    surface = by_kind_key[("pma", thread_id)]
    assert surface["lifecycle"] == "queued"
    assert surface["metadata"]["runtime_status"] == "completed"
    assert surface["metadata"]["latest_execution_status"] == "queued"
    assert surface["metadata"]["active_turn_id"] == f"{thread_id}-turn-b"


def test_pma_compat_snapshot_keeps_completed_runtime_over_stale_running_event(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-completed-stale-running",
        runtime_status="completed",
    )
    SQLiteChatSurfaceEventJournal(hub_root, durable=False).append_event(
        idempotency_key="completed-stale-running",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key="thread-completed-stale-running",
        managed_thread_id="thread-completed-stale-running",
        repo_id="repo-1",
        status="running",
        occurred_at="2026-05-11T00:00:10Z",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).pma_compat_snapshot()
    by_thread_id = {
        thread["managed_thread_id"]: thread for thread in snapshot["threads"]
    }

    thread = by_thread_id["thread-completed-stale-running"]
    assert thread["runtime_status"] == "completed"
    assert thread["normalized_status"] == "completed"
    assert thread["status"] == "completed"


def test_chat_index_treats_ok_runtime_as_terminal_over_stale_running_event(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-ok-stale-running",
        runtime_status="ok",
    )
    SQLiteChatSurfaceEventJournal(hub_root, durable=False).append_event(
        idempotency_key="ok-stale-running",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key="thread-ok-stale-running",
        managed_thread_id="thread-ok-stale-running",
        repo_id="repo-1",
        status="running",
        occurred_at="2026-05-11T00:00:10Z",
    )

    active = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="active",
        limit=20,
    )
    pma_snapshot = ChatSurfaceReadService(hub_root, durable=False).pma_compat_snapshot()
    by_thread_id = {
        thread["managed_thread_id"]: thread for thread in pma_snapshot["threads"]
    }

    assert active["rows"] == []
    assert by_thread_id["thread-ok-stale-running"]["runtime_status"] == "ok"


def test_chat_index_carries_ticket_flow_metadata_for_grouping(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="completed",
        metadata={
            "flow_type": "ticket_flow",
            "thread_kind": "ticket_flow",
            "ticket_id": "TICKET-015",
            "run_id": "run-015",
        },
    )
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow-stale-lifecycle",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="completed",
        metadata={
            "flow_type": "ticket_flow",
            "thread_kind": "ticket_flow",
            "ticket_id": "TICKET-016",
            "run_id": "run-015",
        },
    )
    _seed_execution(
        hub_root,
        thread_id="thread-ticket-flow",
        status="completed",
        prompt_text="Newer visible ticket turn",
        metadata={"user_visible_text": "Newer visible ticket turn"},
        created_at="2026-05-11T00:02:00Z",
    )
    _seed_execution(
        hub_root,
        thread_id="thread-ticket-flow-stale-lifecycle",
        status="completed",
        prompt_text="Older visible ticket turn",
        metadata={"user_visible_text": "Older visible ticket turn"},
        created_at="2026-05-11T00:01:00Z",
    )
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            "UPDATE orch_thread_targets SET updated_at = ? WHERE thread_target_id = ?",
            (
                "2026-05-11T00:05:00Z",
                "thread-ticket-flow-stale-lifecycle",
            ),
        )
    SQLiteChatSurfaceEventJournal(hub_root, durable=False).append_event(
        idempotency_key="stale-running-progress",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key="thread-ticket-flow",
        managed_thread_id="thread-ticket-flow",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        status="running",
        occurred_at="2026-05-11T00:00:10Z",
    )

    index = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="all",
        limit=20,
    )

    row = next(
        item
        for item in index["rows"]
        if item["managed_thread_id"] == "thread-ticket-flow"
    )
    assert row["ticket_id"] == "TICKET-015"
    assert row["run_id"] == "run-015"
    assert row["group_id"] == "run:run-015"

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )

    assert grouped["rows"][0]["row_type"] == "group"
    assert grouped["rows"][0]["group_id"] == "run:run-015"
    assert grouped["rows"][0]["child_count"] == 2
    assert grouped["rows"][0]["last_sort_activity_at"] == "2026-05-11T00:02:00Z"
    assert grouped["rows"][0]["last_lifecycle_update_at"] == "2026-05-11T00:05:00Z"
    assert grouped["rows"][0]["updated_at"] == "2026-05-11T00:02:00Z"
    assert (
        grouped["rows"][0]["debug"]["activity"]["selected_source"]
        == "last_sort_activity_at"
    )

    active = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="active",
        group_by="ticket_run",
        limit=20,
    )
    assert active["rows"] == []


def test_chat_index_ticket_run_group_counts_ticket_flow_child_progress(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    for index in range(1, 6):
        status = "completed" if index <= 3 else "running"
        _seed_thread(
            hub_root,
            thread_id=f"thread-ticket-flow-{index}",
            repo_id="repo-1",
            resource_kind="worktree",
            resource_id="repo-1--ticket-flow",
            runtime_status=status,
            metadata=ticket_flow_thread_metadata(
                flow_run_id="run-100",
                ticket_id=f"TICKET-{index:03d}",
                workspace_root=str(repo_root),
                ticket_path=f".codex-autorunner/tickets/TICKET-{index:03d}.md",
            ),
        )

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )

    assert grouped["rows"] == [
        {
            **grouped["rows"][0],
            "row_type": "group",
            "kind": "ticket_run_group",
            "group_id": "run:run-100",
            "run_id": "run-100",
            "child_count": 5,
            "total_count": 5,
            "done_count": 3,
            "running_count": 2,
            "waiting_count": 0,
            "failed_count": 0,
            "status": "running",
        }
    ]


def test_chat_index_ticket_run_group_ignores_generic_completed_ticket_chat(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-generic-ticket",
        repo_id="repo-1",
        resource_kind="ticket",
        resource_id="TICKET-999",
        runtime_status="completed",
    )

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )

    assert grouped["rows"] == []


def test_chat_index_ticket_file_frontmatter_wins_over_thread_state(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    ticket_path = repo_root / ".codex-autorunner" / "tickets" / "TICKET-010.md"
    _write_ticket(ticket_path, done=False)
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow-conflict",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="completed",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-101",
            ticket_id="TICKET-010",
            workspace_root=str(repo_root),
            ticket_path=".codex-autorunner/tickets/TICKET-010.md",
            extra={"ticket_done": True, "ticket_status": "done"},
        ),
    )

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )
    child = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        parent_group_id="run:run-101",
        limit=20,
    )["rows"][0]

    assert child["ticket_done"] is False
    assert child["ticket_status"] == "unknown"
    assert grouped["rows"][0]["done_count"] == 0


def test_chat_index_ticket_flow_store_fills_missing_ticket_file_progress(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    _seed_ticket_flow_projection(
        hub_root,
        run_id="run-102",
        status="running",
        current_ticket=".codex-autorunner/tickets/TICKET-011.md",
        current_ticket_done=True,
    )
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow-store",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="running",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-102",
            ticket_id="TICKET-011",
            workspace_root=str(repo_root),
            ticket_path=".codex-autorunner/tickets/TICKET-011.md",
        ),
    )

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )
    child = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        parent_group_id="run:run-102",
        limit=20,
    )["rows"][0]

    assert child["ticket_done"] is True
    assert child["ticket_status"] == "done"
    assert child["ticket_progress_source"] == "flow_store"
    assert grouped["rows"][0]["done_count"] == 1


def test_chat_index_ticket_file_frontmatter_wins_over_flow_store_state(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    ticket_path = repo_root / ".codex-autorunner" / "tickets" / "TICKET-012.md"
    _write_ticket(ticket_path, done=False)
    _seed_ticket_flow_projection(
        hub_root,
        run_id="run-103",
        status="completed",
        current_ticket=".codex-autorunner/tickets/TICKET-012.md",
        current_ticket_done=True,
    )
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow-store-conflict",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="completed",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-103",
            ticket_id="TICKET-012",
            workspace_root=str(repo_root),
            ticket_path=".codex-autorunner/tickets/TICKET-012.md",
        ),
    )

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )
    child = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        parent_group_id="run:run-103",
        limit=20,
    )["rows"][0]

    assert child["ticket_done"] is False
    assert child["ticket_status"] == "unknown"
    assert child["ticket_progress_source"] == "ticket_file"
    assert grouped["rows"][0]["done_count"] == 0


def test_chat_index_ticket_flow_store_preserves_failed_status_for_not_done_ticket(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    _seed_ticket_flow_projection(
        hub_root,
        run_id="run-104",
        status="failed",
        current_ticket=".codex-autorunner/tickets/TICKET-013.md",
        current_ticket_done=False,
    )
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow-store-failed",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="running",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-104",
            ticket_id="TICKET-013",
            workspace_root=str(repo_root),
            ticket_path=".codex-autorunner/tickets/TICKET-013.md",
        ),
    )

    child = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        parent_group_id="run:run-104",
        limit=20,
    )["rows"][0]

    assert child["ticket_done"] is False
    assert child["ticket_status"] == "failed"
    assert child["ticket_progress_source"] == "flow_store"


def test_chat_index_ticket_flow_store_reads_repo_flow_store_when_projection_missing(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    _seed_repo_flow_store_ticket_run(
        repo_root,
        run_id="run-105",
        status=FlowRunStatus.RUNNING,
        current_ticket=".codex-autorunner/tickets/TICKET-014.md",
        current_ticket_done=True,
    )
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow-local-store",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="running",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-105",
            ticket_id="TICKET-014",
            workspace_root=str(repo_root),
            ticket_path=".codex-autorunner/tickets/TICKET-014.md",
        ),
    )

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )
    child = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        parent_group_id="run:run-105",
        limit=20,
    )["rows"][0]

    assert child["ticket_done"] is True
    assert child["ticket_status"] == "done"
    assert child["ticket_progress_source"] == "flow_store"
    assert grouped["rows"][0]["done_count"] == 1


def test_chat_index_ticket_flow_projection_rejects_wrong_scope_before_local_fallback(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    wrong_repo_root = tmp_path / "wrong-repo"
    repo_root = tmp_path / "repo"
    _seed_ticket_flow_projection(
        hub_root,
        run_id="run-reused",
        repo_id="repo-wrong",
        workspace_root=wrong_repo_root,
        status="running",
        current_ticket=".codex-autorunner/tickets/TICKET-015.md",
        current_ticket_done=True,
    )
    _seed_repo_flow_store_ticket_run(
        repo_root,
        run_id="run-reused",
        status=FlowRunStatus.RUNNING,
        current_ticket=".codex-autorunner/tickets/TICKET-015.md",
        current_ticket_done=False,
    )
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow-scoped",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="running",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-reused",
            ticket_id="TICKET-015",
            workspace_root=str(repo_root),
            ticket_path=".codex-autorunner/tickets/TICKET-015.md",
        ),
    )

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )
    child = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        parent_group_id="run:run-reused",
        limit=20,
    )["rows"][0]

    assert child["ticket_done"] is False
    assert child["ticket_status"] == "running"
    assert child["ticket_progress_source"] == "flow_store"
    assert grouped["rows"][0]["done_count"] == 0


def test_chat_index_ticket_flow_store_wins_over_stale_hub_projection(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    _seed_ticket_flow_projection(
        hub_root,
        run_id="run-106",
        repo_id="repo-1",
        workspace_root=repo_root,
        status="running",
        current_ticket=".codex-autorunner/tickets/TICKET-016.md",
        current_ticket_done=False,
    )
    _seed_repo_flow_store_ticket_run(
        repo_root,
        run_id="run-106",
        status=FlowRunStatus.RUNNING,
        current_ticket=".codex-autorunner/tickets/TICKET-016.md",
        current_ticket_done=True,
    )
    _seed_thread(
        hub_root,
        thread_id="thread-ticket-flow-stale-projection",
        repo_id="repo-1",
        resource_kind="worktree",
        resource_id="repo-1--ticket-flow",
        runtime_status="running",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-106",
            ticket_id="TICKET-016",
            workspace_root=str(repo_root),
            ticket_path=".codex-autorunner/tickets/TICKET-016.md",
        ),
    )

    grouped = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        limit=20,
    )
    child = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="ticket_run",
        group_by="ticket_run",
        parent_group_id="run:run-106",
        limit=20,
    )["rows"][0]

    assert child["ticket_done"] is True
    assert child["ticket_status"] == "done"
    assert child["ticket_progress_source"] == "flow_store"
    assert grouped["rows"][0]["done_count"] == 1


def test_chat_index_sorts_by_conversation_activity_not_metadata_hydration(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(hub_root, thread_id="thread-older")
    _seed_thread(hub_root, thread_id="thread-newer")
    _seed_execution(
        hub_root,
        thread_id="thread-older",
        status="ok",
        prompt_text="<injected context>runtime notes</injected context>\n\nOlder visible message",
        created_at="2026-05-11T00:01:00Z",
    )
    _seed_execution(
        hub_root,
        thread_id="thread-newer",
        status="ok",
        prompt_text="Newer visible message",
        metadata={"user_visible_text": "Newer visible message"},
        created_at="2026-05-11T00:02:00Z",
    )
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            "UPDATE orch_thread_targets SET updated_at = ? WHERE thread_target_id = ?",
            ("2026-05-11T00:05:00Z", "thread-older"),
        )
        conn.execute(
            "UPDATE orch_thread_targets SET display_name = ? WHERE thread_target_id = ?",
            ("discord:channel-older", "thread-older"),
        )
        conn.execute(
            "UPDATE orch_thread_targets SET updated_at = ? WHERE thread_target_id = ?",
            ("2026-05-11T00:00:10Z", "thread-newer"),
        )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="channel-older",
        thread_target_id="thread-older",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "discord:channel-older"},
    )
    ChannelDirectoryStore(hub_root).record_seen(
        "discord",
        "channel-older",
        None,
        "Agent Nexus / #codex",
    )

    index = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        limit=20
    )

    assert [row["managed_thread_id"] for row in index["rows"][:2]] == [
        "thread-newer",
        "thread-older",
    ]
    older = next(
        row for row in index["rows"] if row["managed_thread_id"] == "thread-older"
    )
    assert older["title"] == "Older visible message"
    assert older["last_activity_at"] == "2026-05-11T00:01:00Z"
    assert older["last_visible_message_at"] == "2026-05-11T00:01:00Z"
    assert older["last_lifecycle_update_at"] == "2026-05-11T00:05:00Z"
    assert older["last_internal_update_at"] > older["last_lifecycle_update_at"]
    assert older["last_sort_activity_at"] == "2026-05-11T00:01:00Z"


def test_chat_index_persists_explicit_activity_clocks_in_sql_projection(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(hub_root, thread_id="thread-clock")
    _seed_execution(
        hub_root,
        thread_id="thread-clock",
        status="completed",
        prompt_text="Visible clock source",
        created_at="2026-05-11T00:01:00Z",
    )
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            "UPDATE orch_thread_targets SET updated_at = ? WHERE thread_target_id = ?",
            ("2026-05-11T00:05:00Z", "thread-clock"),
        )

    service = ChatSurfaceReadService(hub_root, durable=False)
    service.rebuild_chat_index_projection()

    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        row = conn.execute(
            """
            SELECT last_visible_message_at,
                   last_lifecycle_update_at,
                   last_internal_update_at,
                   last_sort_activity_at,
                   last_activity_at,
                   updated_at
              FROM orch_chat_index_projection
             WHERE managed_thread_id = ?
            """,
            ("thread-clock",),
        ).fetchone()

    assert dict(row) == {
        "last_visible_message_at": "2026-05-11T00:01:00Z",
        "last_lifecycle_update_at": "2026-05-11T00:05:00Z",
        "last_internal_update_at": "2026-05-11T00:05:00Z",
        "last_sort_activity_at": "2026-05-11T00:01:00Z",
        "last_activity_at": "2026-05-11T00:01:00Z",
        "updated_at": "2026-05-11T00:05:00Z",
    }


def test_chat_index_queued_visible_turn_sets_sort_clock_without_thread_churn(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(hub_root, thread_id="thread-queued")
    _seed_execution(
        hub_root,
        thread_id="thread-queued",
        status="queued",
        prompt_text="Queued visible message",
        metadata={"user_visible_text": "Queued visible message"},
        created_at="2026-05-11T00:02:00Z",
    )
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            "UPDATE orch_thread_targets SET updated_at = ? WHERE thread_target_id = ?",
            ("2026-05-11T00:00:10Z", "thread-queued"),
        )

    row = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(limit=20)[
        "rows"
    ][0]

    assert row["last_visible_message_at"] == "2026-05-11T00:02:00Z"
    assert row["last_sort_activity_at"] == "2026-05-11T00:02:00Z"
    assert row["last_activity_at"] == "2026-05-11T00:02:00Z"
    assert row["queue_depth"] == 1


def test_chat_index_snapshot_reads_rebuilt_sql_projection_without_reprojecting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(hub_root, thread_id="thread-idle")
    _seed_thread(hub_root, thread_id="thread-running", runtime_status="running")
    _seed_thread(
        hub_root,
        thread_id="thread-archived",
        lifecycle_status="archived",
        runtime_status="completed",
    )
    service = ChatSurfaceReadService(hub_root, durable=False)
    service.rebuild_chat_index_projection()

    def fail_projected_surfaces(*_args, **_kwargs):
        raise AssertionError("chat index snapshot should read SQL projection")

    monkeypatch.setattr(service, "_projected_surfaces", fail_projected_surfaces)

    all_rows = service.chat_index_snapshot(view="all", limit=1)
    assert len(all_rows["rows"]) == 1
    assert all_rows["counters"]["total"] == 2
    assert all_rows["counters"]["running"] == 1
    assert all_rows["counters"]["archived"] == 1
    assert all_rows["window"]["total_count"] == 2

    active = service.chat_index_snapshot(view="active", limit=20)
    assert [row["managed_thread_id"] for row in active["rows"]] == ["thread-running"]
    assert active["rows"][0]["effective_status"] == "running"

    archived = service.chat_index_snapshot(view="archived", limit=20)
    assert [row["managed_thread_id"] for row in archived["rows"]] == ["thread-archived"]
    assert archived["rows"][0]["effective_status"] == "archived"


def test_chat_index_rebuild_repairs_stale_archived_bound_surface_projection(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    bindings = OrchestrationBindingStore(hub_root, durable=False)
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=False)

    for surface_kind, surface_key, active_thread_id in (
        ("discord", "guild:channel", "discord-thread-new"),
        ("telegram", "-1001:77", "telegram-thread-new"),
    ):
        archived_thread_id = f"{surface_kind}-thread-old"
        _seed_thread(
            hub_root,
            thread_id=archived_thread_id,
            lifecycle_status="archived",
            runtime_status="completed",
        )
        _seed_thread(
            hub_root,
            thread_id=active_thread_id,
            lifecycle_status="active",
            runtime_status="running",
        )
        first = bindings.upsert_binding(
            surface_kind=surface_kind,
            surface_key=surface_key,
            thread_target_id=archived_thread_id,
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            metadata={"display_name": f"{surface_kind} chat"},
        )
        bindings.disable_binding(binding_id=first.binding_id)
        bindings.upsert_binding(
            surface_kind=surface_kind,
            surface_key=surface_key,
            thread_target_id=active_thread_id,
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            metadata={"display_name": f"{surface_kind} chat"},
        )
        journal.append_event(
            idempotency_key=f"stale-archive-{surface_kind}",
            event_type="surface.archived",
            surface_kind=surface_kind,
            surface_key=surface_key,
            managed_thread_id=archived_thread_id,
            lifecycle_status="archived",
            status="archived",
            source_kind="test",
            occurred_at="2026-05-11T00:00:01Z",
        )

    service = ChatSurfaceReadService(hub_root, durable=False)
    service.rebuild_chat_index_projection()
    _mark_projected_thread_archived(hub_root, "discord-thread-new")
    _mark_projected_thread_archived(hub_root, "telegram-thread-new")

    assert service.chat_index_projection_status()["needs_rebuild"] is False
    assert (
        service.chat_index_snapshot(view="all", surface_kind="discord", limit=20)[
            "rows"
        ]
        == []
    )
    assert (
        service.chat_index_snapshot(view="all", surface_kind="telegram", limit=20)[
            "rows"
        ]
        == []
    )

    dry_run = service.repair_stale_bound_surface_archive_state(dry_run=True)
    first_repair = service.repair_stale_bound_surface_archive_state()
    second_repair = service.repair_stale_bound_surface_archive_state()

    assert dry_run["matched"] == 2
    assert first_repair["matched"] == 2
    assert first_repair["repaired"] == 2
    assert first_repair["projection"]["row_count"] == 4
    assert second_repair["matched"] == 0
    assert second_repair["repaired"] == 0
    discord = service.chat_index_snapshot(
        view="all",
        surface_kind="discord",
        limit=20,
    )
    telegram = service.chat_index_snapshot(
        view="all",
        surface_kind="telegram",
        limit=20,
    )
    archived = service.chat_index_snapshot(view="archived", limit=20)

    assert [row["managed_thread_id"] for row in discord["rows"]] == [
        "discord-thread-new"
    ]
    assert discord["rows"][0]["archive_state"] == "active"
    assert [row["managed_thread_id"] for row in telegram["rows"]] == [
        "telegram-thread-new"
    ]
    assert telegram["rows"][0]["archive_state"] == "active"
    assert {row["managed_thread_id"] for row in archived["rows"]} == {
        "discord-thread-old",
        "telegram-thread-old",
    }


def test_chat_index_and_detail_keep_bound_chat_display_secondary(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False, migrate=True) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id,
                agent_id,
                repo_id,
                resource_kind,
                resource_id,
                display_name,
                lifecycle_status,
                runtime_status,
                metadata_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "thread-discord-friendly",
                "codex",
                "repo-1",
                "repo",
                "repo-1",
                "discord:1495134681929355404",
                "active",
                "idle",
                "{}",
                "2026-05-11T00:00:00Z",
                "2026-05-11T00:00:10Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id,
                agent_id,
                repo_id,
                resource_kind,
                resource_id,
                display_name,
                lifecycle_status,
                runtime_status,
                metadata_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "thread-discord-custom",
                "codex",
                "repo-1",
                "repo",
                "repo-1",
                "Customer escalation",
                "active",
                "idle",
                "{}",
                "2026-05-11T00:01:00Z",
                "2026-05-11T00:01:10Z",
            ),
        )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="1495134681929355404",
        thread_target_id="thread-discord-friendly",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Agent Nexus / #codex"},
    )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="1495134681929355405",
        thread_target_id="thread-discord-custom",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Agent Nexus / #support"},
    )

    service = ChatSurfaceReadService(hub_root, durable=False)
    index = service.chat_index_snapshot(limit=20)
    row = next(
        item
        for item in index["rows"]
        if item["managed_thread_id"] == "thread-discord-friendly"
    )
    assert row["title"] == "Agent Nexus / #codex"
    assert row["display_title"] == "Agent Nexus / #codex"
    assert row["chat_display_name"] == "Agent Nexus / #codex"
    assert row["binding_display_name"] == "Agent Nexus / #codex"

    detail = service.chat_detail_snapshot("thread-discord-friendly")
    assert detail["thread"]["title"] == "Agent Nexus / #codex"
    assert detail["thread"]["chat_display_name"] == "Agent Nexus / #codex"

    custom_detail = service.chat_detail_snapshot("thread-discord-custom")
    assert custom_detail["thread"]["title"] == "Customer escalation"
    assert custom_detail["thread"]["chat_display_name"] == "Customer escalation"


def test_chat_index_keeps_pma_title_with_notification_reply_contexts(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-notified",
        display_name="Deploy investigation",
    )
    notifications = PmaNotificationStore(hub_root)
    notifications.record_notification(
        correlation_id="corr-notification-source",
        source_kind="pma",
        delivery_mode="direct",
        surface_kind="discord",
        surface_key="guild:alerts",
        delivery_record_id="delivery-notification-source",
        repo_id="repo-1",
        managed_thread_id="thread-notified",
        notification_id="5dd3d434ee364219af850bf41221c27c",
    )
    notifications.record_notification(
        correlation_id="corr-notification-continuation",
        source_kind="pma",
        delivery_mode="direct",
        surface_kind="telegram",
        surface_key="-100:42",
        delivery_record_id="delivery-notification-continuation",
        repo_id="repo-1",
        managed_thread_id="source-thread",
        continuation_thread_target_id="thread-notified",
        notification_id="followup-notification",
    )

    row = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(limit=20)[
        "rows"
    ][0]

    assert row["managed_thread_id"] == "thread-notified"
    assert row["title"] == "Deploy investigation"
    assert row["display_title"] == "Deploy investigation"
    assert row["chat_display_name"] == "Deploy investigation"
    assert (
        "Notification 5dd3d434ee364219af850bf41221c27c" in row["binding_display_names"]
    )
    assert {surface["surface_kind"] for surface in row["surface_bindings"]} >= {
        "pma",
        "notification",
    }


def test_chat_index_keeps_external_notification_label_without_managed_thread(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    PmaNotificationStore(hub_root).record_notification(
        correlation_id="corr-external-notification",
        source_kind="pma",
        delivery_mode="direct",
        surface_kind="discord",
        surface_key="guild:alerts",
        delivery_record_id="delivery-external-notification",
        repo_id="repo-1",
        notification_id="external-notification",
    )

    row = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(
        view="external",
        limit=20,
    )["rows"][0]

    assert row["managed_thread_id"] is None
    assert row["title"] == "Notification external-notification"
    assert row["display_title"] == "Notification external-notification"
    assert row["chat_display_name"] == "Notification external-notification"


def test_chat_index_uses_message_preview_before_thread_id_fallback(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-preview-title",
        last_message_preview="Investigate failed deploy",
    )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="1495134681929355404",
        thread_target_id="thread-preview-title",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Agent Nexus / #deploys"},
    )

    row = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(limit=20)[
        "rows"
    ][0]

    assert row["title"] == "Investigate failed deploy"
    assert row["display_title"] == "Investigate failed deploy"
    assert row["binding_display_name"] == "Agent Nexus / #deploys"


def test_chat_index_uses_provider_title_before_visible_seed(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-provider-title",
        display_name="New PMA chat",
        last_message_preview="First visible request",
        metadata={"provider_conversation_title": "Native runtime title"},
    )

    row = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(limit=20)[
        "rows"
    ][0]

    assert row["title"] == "Native runtime title"
    assert row["display_title"] == "Native runtime title"


def test_chat_index_deprioritizes_uuid_and_ticket_flow_control_prompt_titles(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="12345678-1234-5678-1234-567812345678",
        display_name="12345678-1234-5678-1234-567812345678",
        last_message_preview=(
            "<CAR_TICKET_FLOW_PROMPT><CAR_CURRENT_TICKET_FILE>"
            "PATH: .codex-autorunner/tickets/TICKET-002.md"
            "</CAR_CURRENT_TICKET_FILE></CAR_TICKET_FLOW_PROMPT>"
        ),
        metadata={"ticket_id": "TICKET-002"},
    )

    row = ChatSurfaceReadService(hub_root, durable=False).chat_index_snapshot(limit=20)[
        "rows"
    ][0]

    assert row["title"] == "Ticket flow · TICKET-002"
    assert row["technical_title"] == "12345678-1234-5678-1234-567812345678"


def test_chat_surface_read_model_allows_lifecycle_recovery_events(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    journal = SQLiteChatSurfaceEventJournal(hub_root, durable=False)
    journal.append_event(
        idempotency_key="recovering:failed",
        event_type="execution.progress",
        surface_kind="telegram",
        surface_key="-100:1",
        managed_thread_id="thread-recovering",
        repo_id="repo-1",
        status="failed",
    )
    journal.append_event(
        idempotency_key="recovering:running",
        event_type="execution.progress",
        surface_kind="telegram",
        surface_key="-100:1",
        managed_thread_id="thread-recovering",
        repo_id="repo-1",
        status="running",
    )
    journal.append_event(
        idempotency_key="completed:failed",
        event_type="execution.progress",
        surface_kind="discord",
        surface_key="guild:done",
        managed_thread_id="thread-completed",
        repo_id="repo-1",
        status="failed",
    )
    journal.append_event(
        idempotency_key="completed:delivered",
        event_type="delivery.status_changed",
        surface_kind="discord",
        surface_key="guild:done",
        managed_thread_id="thread-completed",
        repo_id="repo-1",
        status="delivered",
    )
    _seed_thread(hub_root, thread_id="thread-still-failed")
    _seed_execution(hub_root, thread_id="thread-still-failed", status="failed")
    journal.append_event(
        idempotency_key="still-failed:old-bound",
        event_type="surface.bound",
        surface_kind="pma",
        surface_key="thread-still-failed",
        managed_thread_id="thread-still-failed",
        repo_id="repo-1",
        status="bound",
        occurred_at="2026-05-10T00:00:00Z",
    )
    _seed_thread(hub_root, thread_id="thread-pma-recovered")
    _seed_execution(hub_root, thread_id="thread-pma-recovered", status="failed")
    journal.append_event(
        idempotency_key="pma-recovered:running",
        event_type="execution.progress",
        surface_kind="pma",
        surface_key="thread-pma-recovered",
        managed_thread_id="thread-pma-recovered",
        repo_id="repo-1",
        status="running",
        occurred_at="2026-05-11T00:02:00Z",
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).snapshot()
    by_kind_key = {
        (surface["surface_kind"], surface["surface_key"]): surface
        for surface in snapshot["surfaces"]
    }

    assert by_kind_key[("telegram", "-100:1")]["lifecycle"] == "running"
    assert by_kind_key[("discord", "guild:done")]["lifecycle"] == "idle"
    assert by_kind_key[("pma", "thread-still-failed")]["lifecycle"] == "failed"
    pma_snapshot = ChatSurfaceReadService(hub_root, durable=False).pma_compat_snapshot()
    by_thread_id = {
        thread["managed_thread_id"]: thread for thread in pma_snapshot["threads"]
    }
    assert by_thread_id["thread-pma-recovered"]["normalized_status"] == "running"


def test_chat_surface_read_model_builds_pma_compat_snapshot(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(hub_root, thread_id="thread-pma", runtime_status="idle")
    _seed_execution(hub_root, thread_id="thread-pma", status="queued")
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="guild:channel",
        thread_target_id="thread-pma",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Discord channel"},
    )

    snapshot = ChatSurfaceReadService(hub_root, durable=False).pma_compat_snapshot()

    assert snapshot["contract_version"] == "pma_chat_events.v1"
    assert snapshot["cursor"] == 1
    assert len(snapshot["revision"]) == 64
    assert snapshot["threads"] == [
        {
            "managed_thread_id": "thread-pma",
            "agent": "codex",
            "agent_profile": None,
            "repo_id": "repo-1",
            "resource_kind": "repo",
            "resource_id": "repo-1",
            "workspace_root": None,
            "name": "Discord channel",
            "display_title": "Discord channel",
            "technical_title": "thread-pma",
            "model": None,
            "backend_thread_id": None,
            "lifecycle_status": "active",
            "runtime_status": "queued",
            "normalized_status": "queued",
            "status": "queued",
            "target_runtime_status": "idle",
            "execution_status": "queued",
            "active_turn_id": "exec-thread-pma-queued",
            "queued_count": 1,
            "status_reason": None,
            "status_changed_at": None,
            "status_terminal": False,
            "status_turn_id": None,
            "last_turn_id": None,
            "last_message_preview": None,
            "compact_seed": None,
            "accepts_messages": True,
            "updated_at": "2026-05-11T00:01:00Z",
            "created_at": "2026-05-11T00:00:00Z",
            "operator_status": "queued",
            "is_reusable": False,
            "chat_bound": True,
            "binding_kind": "discord",
            "binding_id": "guild:channel",
            "chat_display_name": "Discord channel",
            "binding_count": 1,
            "binding_kinds": ["discord"],
            "binding_ids": ["guild:channel"],
            "chat_display_names": ["Discord channel"],
            "cleanup_protected": False,
        }
    ]


def test_chat_read_model_contract_matrix_documents_shared_semantics(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _seed_thread(
        hub_root,
        thread_id="thread-pma",
        display_name="Web PMA chat",
        last_message_preview="Web visible request",
    )
    _seed_execution(
        hub_root,
        thread_id="thread-pma",
        status="ok",
        metadata={"user_visible_text": "Web visible request"},
        created_at="2026-05-11T00:01:00Z",
    )
    _seed_thread(
        hub_root,
        thread_id="thread-discord",
        display_name="Thread thread-discord",
        metadata={"provider_conversation_title": "Discord deploy triage"},
    )
    OrchestrationBindingStore(hub_root, durable=False).upsert_binding(
        surface_kind="discord",
        surface_key="guild:deploy",
        thread_target_id="thread-discord",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        metadata={"display_name": "Agent Nexus / #deploy"},
    )
    _seed_thread(
        hub_root,
        thread_id="thread-ticket",
        display_name="ticket-flow:codex",
        resource_kind="ticket",
        resource_id="TICKET-005",
        metadata=ticket_flow_thread_metadata(
            flow_run_id="run-005",
            ticket_id="TICKET-005",
            workspace_root=str(hub_root),
        ),
    )
    _seed_thread(hub_root, thread_id="thread-queued", display_name="Queued work")
    _seed_execution(
        hub_root,
        thread_id="thread-queued",
        status="queued",
        metadata={"user_visible_text": "Queued visible request"},
        created_at="2026-05-11T00:02:00Z",
    )
    _seed_thread(
        hub_root,
        thread_id="thread-archived",
        display_name="Archived chat",
        lifecycle_status="archived",
        runtime_status="completed",
    )
    ChannelDirectoryStore(hub_root).record_seen(
        "discord",
        "guild-external",
        None,
        "External diagnostics channel",
    )

    service = ChatSurfaceReadService(hub_root, durable=False)
    active_rows = {
        row["managed_thread_id"]: row
        for row in service.chat_index_snapshot(view="all", limit=20)["rows"]
    }
    archived_rows = {
        row["managed_thread_id"]: row
        for row in service.chat_index_snapshot(view="archived", limit=20)["rows"]
    }
    external_rows = {
        row["chat_id"]: row
        for row in service.chat_index_snapshot(view="external", limit=20)["rows"]
    }
    matrix = {
        "web_pma": active_rows["thread-pma"],
        "discord_bound": active_rows["thread-discord"],
        "ticket_flow": active_rows["thread-ticket"],
        "queued_only": active_rows["thread-queued"],
        "archived": archived_rows["thread-archived"],
        "external_unbound": external_rows["surface:discord:guild-external"],
    }

    assert matrix["web_pma"]["title"] == "Web PMA chat"
    assert matrix["web_pma"]["last_visible_message_at"] == "2026-05-11T00:01:00Z"
    assert matrix["web_pma"]["last_sort_activity_at"] == "2026-05-11T00:01:00Z"
    assert matrix["discord_bound"]["title"] == "Discord deploy triage"
    assert matrix["discord_bound"]["binding_display_name"] == "Agent Nexus / #deploy"
    assert matrix["ticket_flow"]["group_id"] == "run:run-005"
    assert matrix["queued_only"]["queue_depth"] == 1
    assert matrix["queued_only"]["last_sort_activity_at"] == "2026-05-11T00:02:00Z"
    assert matrix["archived"]["archive_state"] == "archived"
    assert matrix["external_unbound"]["managed_thread_id"] is None
    assert matrix["external_unbound"]["title"] == "External diagnostics channel"

    for row in matrix.values():
        assert row["last_activity_at"] == row["last_sort_activity_at"]
        assert row["debug"]["title"]["selected"] == row["display_title"]
        assert row["debug"]["activity"]["selected"] == row["last_sort_activity_at"]

    pma_thread = next(
        thread
        for thread in service.pma_compat_snapshot()["threads"]
        if thread["managed_thread_id"] == "thread-pma"
    )
    assert pma_thread["updated_at"] == "2026-05-11T00:01:00Z"
    assert matrix["web_pma"]["last_lifecycle_update_at"] == "2026-05-11T00:00:10Z"
