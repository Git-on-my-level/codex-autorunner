from __future__ import annotations

from pathlib import Path

from tests.support.turn_execution import create_test_turn

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
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    ApprovalRequested,
    Completed,
    Failed,
    Interrupted,
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


def _assert_v2_metadata(item: dict) -> None:
    assert item["contract_version"] == "managed_thread_timeline.v3"
    assert item["identity"]["timeline_item_id"] == item["item_id"]
    assert isinstance(item["identity"]["progress_item_ids"], list)
    assert "correlation_id" in item["identity"]
    assert isinstance(item["provenance"]["source_event_ids"], list)
    assert isinstance(item["provenance"]["progress_event_ids"], list)
    assert item["provenance"]["cursor_event_id"] is None


def test_user_message_timeline_projects_capsule_visibility_metadata(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = create_test_turn(
        store,
        thread_id,
        prompt="<injected context>\nrepo guidance\n</injected context>\n\nFix login",
        metadata={
            "raw_model_prompt": (
                "<injected context>\nrepo guidance\n</injected context>\n\nFix login"
            ),
            "user_visible_text": "Fix login",
            "title_seed": "Fix login",
            "capsule_refs": [
                {
                    "capsule_id": "car.repo_basics",
                    "capsule_version": "1",
                    "visibility": "model_only",
                    "scope": "repo",
                    "source_digest": "sha256:repo",
                    "payload_digest": "sha256:payload",
                    "render_decision": "rendered",
                    "reason": "repo_context",
                }
            ],
        },
    )
    turn_id = str(turn["managed_turn_id"])

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    user = next(item for item in payload["items"] if item["kind"] == "user_message")
    assert user["item_id"] == f"turn:{turn_id}:user"
    assert user["payload"]["text"] == "Fix login"
    assert user["payload"]["user_visible_text"] == "Fix login"
    assert user["payload"]["visibility"] == "user_visible"
    assert user["payload"]["raw_model_prompt"].startswith("<injected context>")
    assert user["payload"]["capsule_refs"] == [
        {
            "capsule_id": "car.repo_basics",
            "capsule_version": "1",
            "visibility": "model_only",
            "scope": "repo",
            "source_digest": "sha256:repo",
            "payload_digest": "sha256:payload",
            "render_decision": "rendered",
            "reason": "repo_context",
        }
    ]


def test_completed_timeline_separates_intermediate_and_final_output(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = create_test_turn(
        store,
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
    assert kinds.count("intermediate") == 1
    assert kinds.count("assistant_message") == 1
    assert kinds.count("artifact") == 2
    assert payload["items"][0]["item_id"] == f"turn:{turn_id}:user"
    for item in payload["items"]:
        _assert_v2_metadata(item)
    assistant = next(
        item for item in payload["items"] if item["kind"] == "assistant_message"
    )
    assert assistant["payload"]["text"] == "final answer"
    assert assistant["provenance"]["source_event_ids"] == [3]
    assert assistant["provenance"]["progress_event_ids"] == [3]
    assert not any(
        item["payload"].get("source_event_type") == "output_delta"
        for item in payload["items"]
    )


def test_high_volume_assistant_and_log_deltas_do_not_expand_default_timeline(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = create_test_turn(store, thread_id, prompt="stream a lot")
    turn_id = str(turn["managed_turn_id"])
    events = [
        OutputDelta(
            timestamp=f"2026-05-06T10:00:{index % 60:02d}Z",
            delta_type=(
                RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM
                if index % 2
                else RUN_EVENT_DELTA_TYPE_LOG_LINE
            ),
            content=f"chunk {index}",
        )
        for index in range(1000)
    ]
    events.append(
        Completed(
            timestamp="2026-05-06T10:20:00Z",
            final_message="bounded final",
        )
    )
    persist_turn_timeline(
        hub_root,
        execution_id=turn_id,
        target_kind="thread_target",
        target_id=thread_id,
        events=events,
    )
    assert store.mark_turn_finished(
        turn_id,
        status="ok",
        assistant_text="bounded final",
    )

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    assert _kinds(payload) == ["user_message", "assistant_message", "status"]
    assert payload["item_count"] == 3
    assert payload["projection"]["kind"] == "transcript"
    assert payload["projection"]["raw_trace_available"] is True


def test_running_timeline_projects_progress_tool_group_and_approval(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = create_test_turn(store, thread_id, prompt="run tests")
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
    assert tool["identity"]["progress_item_ids"] == [
        "progress:tool:0002:pytest",
        "progress:tool:0003:pytest",
    ]
    assert tool["provenance"]["source_event_ids"] == [2, 3]
    assert tool["provenance"]["progress_event_ids"] == [2, 3]
    approval = next(item for item in payload["items"] if item["kind"] == "approval")
    assert approval["payload"]["progress_item"]["kind"] == "approval"
    assert approval["identity"]["progress_item_ids"] == ["progress:approval:approval-1"]
    assert approval["provenance"]["source_event_ids"] == [4]
    assert approval["provenance"]["progress_event_ids"] == [4]
    intermediate = next(
        item for item in payload["items"] if item["kind"] == "intermediate"
    )
    assert intermediate["payload"]["source_event_ids"] == [1]
    assert intermediate["identity"]["progress_item_ids"] == ["progress:notice:0001"]
    assert intermediate["provenance"]["source_event_ids"] == [1]
    assert intermediate["provenance"]["progress_event_ids"] == [1]
    assert intermediate["payload"]["detail_available"] is True
    for item in payload["items"]:
        _assert_v2_metadata(item)


def test_queued_user_messages_remain_distinct_and_ordered_while_running(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    active = create_test_turn(store, thread_id, prompt="active turn")
    queued_one = create_test_turn(
        store,
        thread_id,
        prompt="queued one",
        busy_policy="queue",
    )
    queued_two = create_test_turn(
        store,
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
    failed = create_test_turn(store, thread_id, prompt="will fail")
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

    interrupted = create_test_turn(store, thread_id, prompt="will stop")
    interrupted_id = str(interrupted["managed_turn_id"])
    persist_turn_timeline(
        hub_root,
        execution_id=interrupted_id,
        target_kind="thread_target",
        target_id=thread_id,
        events=[
            Interrupted(
                timestamp="2026-05-06T10:00:02Z",
                reason="user stopped the run",
            )
        ],
    )
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
    assert statuses[f"turn:{interrupted_id}:status:interrupted"]["timestamp"] == (
        "2026-05-06T10:00:02Z"
    )
    assert statuses[f"turn:{interrupted_id}:status:interrupted"]["provenance"][
        "source_event_ids"
    ] == [1]


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
    assert item["item_id"] == "turn:turn-1:tool:2:pytest"
    assert item["payload"]["source_event_ids"] == [2]
    assert item["payload"]["detail_available"] is True


def test_live_tail_event_uses_progress_metadata_for_intermediate_titles() -> None:
    item = timeline_item_from_tail_event(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        tail_event={
            "event_id": 3,
            "event_type": "progress",
            "summary": "Starting pytest",
            "title": "Progress",
            "phase": "testing",
            "received_at": "2026-05-06T10:00:03Z",
            "progress_item": {
                "item_id": "progress:notice:0003",
                "kind": "notice",
                "state": "running",
                "title": "Progress",
                "summary": "Starting pytest",
                "event_ids": [3],
            },
            "progress_kind": "notice",
            "progress_state": "running",
        },
    )

    assert item is not None
    assert item["kind"] == "intermediate"
    assert item["payload"]["title"] == "Starting pytest"
    assert item["payload"]["intermediate_kind"] == "progress"


def test_live_tail_event_carries_hidden_progress_metadata() -> None:
    item = timeline_item_from_tail_event(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        tail_event={
            "event_id": 4,
            "event_type": "progress",
            "summary": "terminal=3977ms",
            "received_at": "2026-05-06T10:00:04Z",
            "progress_item": {
                "item_id": "progress:hidden:chat_execution_journal:0004",
                "kind": "hidden",
                "state": "hidden",
                "title": "Hidden progress",
                "summary": None,
                "event_ids": [4],
                "hidden": True,
            },
            "progress_kind": "hidden",
            "progress_state": "hidden",
        },
    )

    assert item is not None
    assert item["kind"] == "intermediate"
    assert item["payload"]["hidden"] is True
    assert item["payload"]["progress_item"]["hidden"] is True


def test_live_tail_event_projects_provider_compaction_run_notice_data() -> None:
    item = timeline_item_from_tail_event(
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        tail_event={
            "event_id": 5,
            "event_type": "progress",
            "summary": "Provider retained key state.",
            "title": "Provider Context Compaction",
            "received_at": "2026-05-06T10:00:05Z",
            "run_notice": {
                "kind": "provider_context_compaction",
                "message": "Provider retained key state.",
                "data": {
                    "provider": "opencode",
                    "summary": "Provider retained key state.",
                },
            },
            "progress_item": {
                "item_id": "progress:notice:0005",
                "kind": "notice",
                "state": "running",
                "title": "Provider Context Compaction",
                "summary": "Provider retained key state.",
                "event_ids": [5],
            },
            "progress_kind": "notice",
            "progress_state": "running",
        },
    )

    assert item is not None
    assert item["kind"] == "lifecycle"
    compaction = item["payload"]["context_compaction"]
    assert compaction["source"] == "provider"
    assert compaction["provider"] == "opencode"
    assert compaction["summary"] == "Provider retained key state."


def test_timeline_includes_delivery_state_items(tmp_path: Path) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = create_test_turn(store, thread_id, prompt="deliver this")
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
            '{"summary_length": 42, "summary": "Keep the current goal.\\nPreserve constraints.", '
            '"summary_preview": "Keep the current goal.", '
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
    assert lifecycle[0]["payload"]["lifecycle_kind"] == "context_compaction"
    assert lifecycle[0]["payload"]["title"] == "Context compacted by CAR"
    compaction = lifecycle[0]["payload"]["context_compaction"]
    assert compaction["source"] == "car"
    assert compaction["summary"] == "Keep the current goal.\nPreserve constraints."
    assert compaction["preview"] == "Keep the current goal."
    assert compaction["scope"] == "managed_thread"
    assert compaction["started_fresh_session"] is True


def test_timeline_projects_provider_native_compaction_notice(tmp_path: Path) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = create_test_turn(store, thread_id, prompt="continue")
    turn_id = str(turn["managed_turn_id"])
    persist_turn_timeline(
        hub_root,
        execution_id=turn_id,
        target_kind="thread_target",
        target_id=thread_id,
        events=[
            RunNotice(
                timestamp="2026-05-06T10:00:00Z",
                kind="provider_context_compaction",
                message="Provider retained key state.",
                data={
                    "provider": "opencode",
                    "summary": "Provider retained key state.",
                },
            )
        ],
    )

    payload = build_managed_thread_timeline(
        hub_root,
        thread_store=store,
        managed_thread_id=thread_id,
    )

    lifecycle = [item for item in payload["items"] if item["kind"] == "lifecycle"]
    assert len(lifecycle) == 1
    assert lifecycle[0]["payload"]["lifecycle_kind"] == "context_compaction"
    assert lifecycle[0]["payload"]["title"] == "Runtime compacted context"
    compaction = lifecycle[0]["payload"]["context_compaction"]
    assert compaction["source"] == "provider"
    assert compaction["provider"] == "opencode"
    assert compaction["summary"] == "Provider retained key state."
    assert compaction["scope"] == "provider_session"
    assert compaction["started_fresh_session"] is False


def test_timeline_projects_equivalent_delivery_state_for_chat_surfaces(
    tmp_path: Path,
) -> None:
    hub_root, store, thread_id = _store(tmp_path)
    turn = create_test_turn(store, thread_id, prompt="deliver everywhere")
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
