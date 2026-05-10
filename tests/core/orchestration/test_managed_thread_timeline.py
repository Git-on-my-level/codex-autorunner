from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryIntent,
    ManagedThreadDeliveryTarget,
    build_managed_thread_delivery_idempotency_key,
    initialize_orchestration_sqlite,
)
from codex_autorunner.core.orchestration.managed_thread_delivery_ledger import (
    SQLiteManagedThreadDeliveryLedger,
)
from codex_autorunner.core.orchestration.managed_thread_timeline import (
    build_managed_thread_timeline,
    timeline_item_from_tail_event,
)
from codex_autorunner.core.orchestration.turn_timeline import persist_turn_timeline
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    ToolCall,
    ToolResult,
)


def _hub_root(tmp_path: Path) -> Path:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    return hub_root


def _store(tmp_path: Path) -> tuple[Path, ManagedThreadStore, str]:
    hub_root = _hub_root(tmp_path)
    workspace = hub_root / "worktree"
    workspace.mkdir(parents=True)
    store = ManagedThreadStore(hub_root)
    thread = store.create_thread("codex", workspace)
    return hub_root, store, str(thread["managed_thread_id"])


def _kinds(payload: dict) -> list[str]:
    return [str(item["kind"]) for item in payload["items"]]


def test_completed_timeline_separates_intermediate_and_final_output(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = store.create_turn(
        thread_id,
        prompt="summarize the repo",
        metadata={
            "attachments": [{"attachment_id": "att-1", "title": "notes.txt"}],
            "artifacts": [{"artifact_id": "dispatch-1", "path": "DISPATCH.md"}],
        },
    )
    turn_id = str(turn["managed_turn_id"])
    persist_turn_timeline(
        hub_root,
        execution_id=turn_id,
        target_kind="thread_target",
        target_id=thread_id,
        events=[
            RunNotice(
                timestamp="2026-05-06T10:00:01Z",
                kind="thinking",
                message="Reading files",
            ),
            OutputDelta(
                timestamp="2026-05-06T10:00:02Z",
                delta_type="assistant_stream",
                content="draft partial",
            ),
            Completed(
                timestamp="2026-05-06T10:00:03Z",
                final_message="final answer",
            ),
        ],
    )
    assert store.mark_turn_finished(turn_id, status="ok", assistant_text="final answer")

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    kinds = _kinds(payload)
    assert kinds.count("user_message") == 1
    assert kinds.count("intermediate") == 2
    assert kinds.count("assistant_message") == 1
    assert kinds.count("artifact") == 2
    assert payload["items"][0]["item_id"] == f"turn:{turn_id}:user"
    assistant = next(
        item for item in payload["items"] if item["kind"] == "assistant_message"
    )
    assert assistant["payload"]["text"] == "final answer"
    assert all(item["kind"] != "assistant_message" for item in payload["items"][1:3])


def test_running_timeline_projects_progress_tool_group_and_approval(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = store.create_turn(thread_id, prompt="run tests")
    turn_id = str(turn["managed_turn_id"])
    persist_turn_timeline(
        hub_root,
        execution_id=turn_id,
        target_kind="thread_target",
        target_id=thread_id,
        events=[
            RunNotice(
                timestamp="2026-05-06T10:00:01Z",
                kind="progress",
                message="Starting pytest",
            ),
            ToolCall(
                timestamp="2026-05-06T10:00:02Z",
                tool_name="pytest",
                tool_input={"path": "tests"},
            ),
            ToolResult(
                timestamp="2026-05-06T10:00:03Z",
                tool_name="pytest",
                status="ok",
                result="passed",
            ),
            ApprovalRequested(
                timestamp="2026-05-06T10:00:04Z",
                request_id="approval-1",
                description="Allow write",
                context={"scope": "workspace"},
            ),
        ],
    )

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    assert "status" in _kinds(payload)
    assert "intermediate" in _kinds(payload)
    assert "tool_group" in _kinds(payload)
    assert "approval" in _kinds(payload)
    assert _kinds(payload) == [
        "user_message",
        "status",
        "intermediate",
        "tool_group",
        "approval",
    ]
    assert {item["item_id"] for item in payload["items"]} >= {
        f"turn:{turn_id}:user",
        f"turn:{turn_id}:status:running",
        f"turn:{turn_id}:approval:approval-1",
    }
    tool = next(item for item in payload["items"] if item["kind"] == "tool_group")
    assert [p["state"] for p in tool["payload"]["progress_items"]] == [
        "started",
        "completed",
    ]
    approval = next(item for item in payload["items"] if item["kind"] == "approval")
    assert approval["payload"]["progress_item"]["kind"] == "approval"
    intermediate = next(
        item for item in payload["items"] if item["kind"] == "intermediate"
    )
    assert intermediate["payload"]["source_event_ids"] == [1]
    assert intermediate["payload"]["detail_available"] is True


def test_queued_user_messages_remain_distinct_and_ordered_while_running(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    active = store.create_turn(thread_id, prompt="active turn")
    queued_one = store.create_turn(
        thread_id,
        prompt="queued one",
        busy_policy="queue",
    )
    queued_two = store.create_turn(
        thread_id,
        prompt="queued two",
        busy_policy="queue",
    )

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )
    user_items = [item for item in payload["items"] if item["kind"] == "user_message"]

    assert [item["payload"]["text"] for item in user_items] == [
        "active turn",
        "queued one",
        "queued two",
    ]
    assert [item["item_id"] for item in user_items] == [
        f"turn:{active['managed_turn_id']}:user",
        f"turn:{queued_one['managed_turn_id']}:user",
        f"turn:{queued_two['managed_turn_id']}:user",
    ]

    assert store.promote_queued_turn(thread_id, str(queued_one["managed_turn_id"]))
    promoted_payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )
    promoted_user_items = [
        item for item in promoted_payload["items"] if item["kind"] == "user_message"
    ]

    assert [item["item_id"] for item in promoted_user_items] == [
        f"turn:{active['managed_turn_id']}:user",
        f"turn:{queued_one['managed_turn_id']}:user",
        f"turn:{queued_two['managed_turn_id']}:user",
    ]


def test_failed_and_interrupted_timelines_include_terminal_status(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    failed = store.create_turn(thread_id, prompt="will fail")
    failed_id = str(failed["managed_turn_id"])
    persist_turn_timeline(
        hub_root,
        execution_id=failed_id,
        target_kind="thread_target",
        target_id=thread_id,
        events=[
            Failed(
                timestamp="2026-05-06T10:00:01Z",
                error_message="boom",
            )
        ],
    )
    assert store.mark_turn_finished(failed_id, status="error", error="boom")

    interrupted = store.create_turn(thread_id, prompt="will stop")
    interrupted_id = str(interrupted["managed_turn_id"])
    assert store.mark_turn_interrupted(interrupted_id)

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    statuses = {
        item["item_id"]: item for item in payload["items"] if item["kind"] == "status"
    }
    assert statuses[f"turn:{failed_id}:status:error"]["payload"]["error"] == "boom"
    assert (
        statuses[f"turn:{interrupted_id}:status:interrupted"]["status"] == "interrupted"
    )


def test_live_tail_event_projects_to_canonical_timeline_item() -> None:
    item = timeline_item_from_tail_event(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        tail_event={
            "event_id": 2,
            "event_type": "tool_completed",
            "summary": "tool: pytest",
            "received_at": "2026-05-06T10:00:03Z",
            "tool_name": "pytest",
            "tool_state": "completed",
            "progress_item": {
                "item_id": "progress:tool:0002:pytest",
                "kind": "tool",
                "state": "completed",
                "title": "pytest",
                "summary": "tool: pytest",
                "event_ids": [2],
                "group_id": "tools:0001:pytest",
                "group_kind": "tool_group",
                "tool_name": "pytest",
            },
            "progress_group_id": "tools:0001:pytest",
            "progress_kind": "tool",
            "progress_state": "completed",
        },
    )

    assert item is not None
    assert item["kind"] == "tool_group"
    assert item["item_id"] == "turn:turn-1:tool:1:pytest"
    assert item["payload"]["source_event_ids"] == [2]
    assert item["payload"]["detail_available"] is True


def test_timeline_includes_delivery_state_items(tmp_path: Path) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = store.create_turn(thread_id, prompt="deliver this")
    turn_id = str(turn["managed_turn_id"])
    assert store.mark_turn_finished(turn_id, status="ok", assistant_text="done")
    ledger = SQLiteManagedThreadDeliveryLedger(hub_root, durable=False)
    ledger.register_intent(
        ManagedThreadDeliveryIntent(
            delivery_id="delivery-1",
            managed_thread_id=thread_id,
            managed_turn_id=turn_id,
            idempotency_key=build_managed_thread_delivery_idempotency_key(
                managed_thread_id=thread_id,
                managed_turn_id=turn_id,
                surface_kind="web",
                surface_key="pma",
            ),
            target=ManagedThreadDeliveryTarget(
                surface_kind="web",
                adapter_key="web",
                surface_key="pma",
            ),
            envelope=ManagedThreadDeliveryEnvelope(
                envelope_version="managed_thread_delivery.v1",
                final_status="ok",
                assistant_text="done",
            ),
        )
    )

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    delivery = [item for item in payload["items"] if item["kind"] == "delivery_state"]
    assert len(delivery) == 1
    assert delivery[0]["item_id"] == "delivery:delivery-1"
    assert delivery[0]["payload"]["state"] == "pending"


def test_timeline_includes_compaction_lifecycle_item(tmp_path: Path) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    store.append_action(
        "managed_thread_compact",
        managed_thread_id=thread_id,
        payload_json=(
            '{"summary_length": 42, "summary_preview": "Keep the current goal.", '
            '"reset_backend": true}'
        ),
    )

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    lifecycle = [item for item in payload["items"] if item["kind"] == "lifecycle"]
    assert len(lifecycle) == 1
    assert lifecycle[0]["item_id"] == "action:1:compact"
    assert lifecycle[0]["payload"]["lifecycle_kind"] == "chat_compacted"
    assert lifecycle[0]["payload"]["title"] == "Chat compacted"
    assert lifecycle[0]["payload"]["summary_preview"] == "Keep the current goal."
    assert lifecycle[0]["payload"]["reset_backend"] is True


def test_timeline_projects_equivalent_delivery_state_for_chat_surfaces(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = store.create_turn(thread_id, prompt="deliver everywhere")
    turn_id = str(turn["managed_turn_id"])
    assert store.mark_turn_finished(turn_id, status="ok", assistant_text="done")
    ledger = SQLiteManagedThreadDeliveryLedger(hub_root, durable=False)
    for surface_kind, surface_key in (
        ("web", thread_id),
        ("discord", "channel-1"),
        ("telegram", "chat-1:55"),
    ):
        ledger.register_intent(
            ManagedThreadDeliveryIntent(
                delivery_id=f"delivery-{surface_kind}",
                managed_thread_id=thread_id,
                managed_turn_id=turn_id,
                idempotency_key=build_managed_thread_delivery_idempotency_key(
                    managed_thread_id=thread_id,
                    managed_turn_id=turn_id,
                    surface_kind=surface_kind,
                    surface_key=surface_key,
                ),
                target=ManagedThreadDeliveryTarget(
                    surface_kind=surface_kind,
                    adapter_key=surface_kind,
                    surface_key=surface_key,
                ),
                envelope=ManagedThreadDeliveryEnvelope(
                    envelope_version="managed_thread_delivery.v1",
                    final_status="ok",
                    assistant_text="done",
                ),
            )
        )

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    deliveries = {
        item["payload"]["surface_kind"]: item
        for item in payload["items"]
        if item["kind"] == "delivery_state"
    }
    assert set(deliveries) == {"web", "discord", "telegram"}
    assert {
        surface_kind: {
            "managed_turn_id": item["managed_turn_id"],
            "state": item["payload"]["state"],
            "final_status": item["payload"]["final_status"],
        }
        for surface_kind, item in deliveries.items()
    } == {
        "web": {
            "managed_turn_id": turn_id,
            "state": "pending",
            "final_status": "ok",
        },
        "discord": {
            "managed_turn_id": turn_id,
            "state": "pending",
            "final_status": "ok",
        },
        "telegram": {
            "managed_turn_id": turn_id,
            "state": "pending",
            "final_status": "ok",
        },
    }
