from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from codex_autorunner.surfaces.web.read_model_contracts import (
    ChatDetailPatch,
    ChatDetailPatchEvent,
    ChatDetailSnapshot,
    ChatFacetCounts,
    ChatFacetRequest,
    ChatIndexCounters,
    ChatIndexFacets,
    ChatIndexPatch,
    ChatIndexPatchEvent,
    ChatIndexRow,
    ChatIndexSnapshot,
    ChatQueueSummary,
    ChatThreadProjection,
    ChatTimelineIdentity,
    ChatTimelineItem,
    ChatTimelineProvenance,
    PageWindow,
    ProjectionCursor,
    ReadModelEventEnvelope,
    RepairPolicy,
    RepoTopology,
    RepoWorktreePatch,
    RepoWorktreePatchEvent,
    RepoWorktreeRuntimeSnapshot,
    RepoWorktreeTopologySnapshot,
    RuntimeProjection,
    TicketDetailPatch,
    TicketDetailPatchEvent,
    TicketDetailSnapshot,
    TicketProjection,
    TicketQueueSibling,
    TicketRunGroup,
    WorktreeTopology,
    dump_read_model_contract,
    load_read_model_contract,
)

NOW = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)


def cursor(sequence: int = 1) -> ProjectionCursor:
    return ProjectionCursor(
        value=f"projection:ui:{sequence}",
        sequence=sequence,
        source="ui_projection_journal",
        issued_at=NOW,
    )


def window() -> PageWindow:
    return PageWindow(limit=50, next_cursor="projection:ui:next", total_estimate=1)


def repair(route: str) -> RepairPolicy:
    return RepairPolicy(snapshot_route=route)


def envelope(
    event_type: str, entity_kind: str, entity_id: str, operation: str
) -> ReadModelEventEnvelope:
    return ReadModelEventEnvelope(
        event_type=event_type,
        cursor=cursor(2),
        entity_kind=entity_kind,
        entity_id=entity_id,
        operation=operation,
        generated_at=NOW,
    )


def chat_row() -> ChatIndexRow:
    return ChatIndexRow(
        chat_id="chat-1",
        surface="pma",
        title="Ticket chat",
        display_title="Release room",
        technical_title="thread:chat-1",
        primary_surface={"surface_kind": "pma", "surface_key": "chat-1"},
        surface_bindings=[
            {
                "surface_kind": "discord",
                "surface_key": "channel-1",
                "display_name": "Release room",
            }
        ],
        binding_display_name="Release room",
        binding_display_names=["Release room"],
        lifecycle="running",
        runtime_status="running",
        effective_status="running",
        archive_state="active",
        status="running",
        unread_count=2,
        last_activity_at=NOW,
        sort_key={"last_activity_desc": -1, "row_id": "thread:chat-1"},
        resource_kind="ticket",
        resource_id="TICKET-001",
        workspace_root="/work/repo",
        repo_id="repo-1",
        worktree_id="wt-1",
        ticket_id="TICKET-001",
        run_id="run-1",
        agent="codex",
        agent_profile="m4-pma",
        chat_kind="coding_agent",
        facets=ChatIndexFacets(
            category="ticket_run",
            turn_kinds=["message", "review"],
            origin_kinds=["surface"],
            transports=["pma", "discord"],
            scope_kind="worktree",
            scope_id="wt-1",
            agent_kind="coding_agent",
        ),
        model="gpt-5.3-codex",
        group_id="ticket-run:run-1",
    )


def test_chat_index_snapshot_and_patch_round_trip_with_camel_case_payloads() -> None:
    snapshot = ChatIndexSnapshot(
        cursor=cursor(),
        window=window(),
        filter="active",
        facet_request=ChatFacetRequest(
            categories=["ticket_run"],
            turn_kinds=["message"],
            transports=["pma", "discord"],
            scope_kinds=["worktree"],
            agent_kinds=["coding_agent"],
        ),
        rows=[chat_row()],
        counters=ChatIndexCounters(total=1, waiting=0, running=1, unread=2, archived=0),
        facet_counts=ChatFacetCounts(
            category={"regular": 120, "ticket_run": 5, "automation": 14, "system": 2},
            turn_kind={"message": 125, "review": 8, "automation": 14},
            origin_kind={"surface": 130, "automation": 14, "system": 2},
            transport={"pma": 204, "discord": 14, "telegram": 1, "notification": 2},
            scope_kind={"hub": 12, "repo": 48, "worktree": 91},
            agent_kind={"pma": 40, "coding_agent": 161},
        ),
        repair=repair("/hub/read-models/chats"),
    )
    payload = dump_read_model_contract(snapshot)

    assert payload["contractVersion"] == "web-read-models.v1"
    assert payload["rows"][0]["chatId"] == "chat-1"
    assert payload["rows"][0]["effectiveStatus"] == "running"
    assert payload["rows"][0]["displayTitle"] == "Release room"
    assert payload["rows"][0]["facets"]["category"] == "ticket_run"
    assert payload["rows"][0]["facets"]["turnKinds"] == ["message", "review"]
    assert payload["facetRequest"]["categories"] == ["ticket_run"]
    assert payload["facetCounts"]["transport"]["discord"] == 14
    assert payload["rows"][0]["surfaceBindings"][0]["surface_kind"] == "discord"
    assert load_read_model_contract(ChatIndexSnapshot, payload) == snapshot

    event = ChatIndexPatchEvent(
        envelope=envelope("chat.index.patch", "chat", "chat-1", "patch"),
        patch=ChatIndexPatch(rows=[chat_row()], counters=snapshot.counters),
    )
    event_payload = dump_read_model_contract(event)

    assert event_payload["envelope"]["eventType"] == "chat.index.patch"
    assert event_payload["patch"]["rows"][0]["unreadCount"] == 2
    assert load_read_model_contract(ChatIndexPatchEvent, event_payload) == event


def test_chat_index_ticket_run_group_contract_round_trip_with_ticket_flow_fields() -> (
    None
):
    rows = [
        chat_row().model_copy(
            update={
                "chat_id": f"chat-{index}",
                "runtime_status": runtime_status,
                "status": status,
                "ticket_id": f"TICKET-00{index}",
                "run_id": "run-1",
                "group_id": "run:run-1",
                "flow_type": "ticket_flow",
                "ticket_done": ticket_done,
                "ticket_status": ticket_status,
            }
        )
        for index, runtime_status, status, ticket_done, ticket_status in [
            (1, "completed", "idle", True, "done"),
            (2, "success", "idle", True, "done"),
            (3, "delivered", "idle", True, "done"),
            (4, "running", "running", False, "running"),
            (5, "running", "running", False, "running"),
        ]
    ]
    snapshot = ChatIndexSnapshot(
        cursor=cursor(),
        window=window(),
        filter="ticket_runs",
        rows=rows,
        groups=[
            TicketRunGroup(
                group_id="run:run-1",
                run_id="run-1",
                scope_kind="worktree",
                scope_id="wt-1",
                label="run:run-1",
                status="running",
                total_count=5,
                done_count=3,
                running_count=2,
                waiting_count=0,
                failed_count=0,
                unread_count=0,
                updated_at=NOW,
            )
        ],
        counters=ChatIndexCounters(total=5, waiting=0, running=2, unread=0, archived=0),
        repair=repair("/hub/read-models/chats"),
    )

    payload = dump_read_model_contract(snapshot)

    assert payload["rows"][0]["flowType"] == "ticket_flow"
    assert payload["rows"][0]["ticketDone"] is True
    assert payload["rows"][0]["ticketStatus"] == "done"
    assert payload["groups"][0]["kind"] == "ticket_run_group"
    assert payload["groups"][0]["doneCount"] == 3
    assert load_read_model_contract(ChatIndexSnapshot, payload) == snapshot


def test_generic_completed_chat_contract_does_not_gain_ticket_flow_fields() -> None:
    row = chat_row().model_copy(
        update={
            "chat_id": "generic-complete",
            "runtime_status": "completed",
            "status": "idle",
            "ticket_id": None,
            "run_id": None,
            "group_id": None,
            "flow_type": None,
            "ticket_done": None,
            "ticket_status": None,
        }
    )
    payload = dump_read_model_contract(
        ChatIndexSnapshot(
            cursor=cursor(),
            window=window(),
            filter="all",
            rows=[row],
            counters=ChatIndexCounters(
                total=1, waiting=0, running=0, unread=0, archived=0
            ),
            repair=repair("/hub/read-models/chats"),
        )
    )

    assert payload["rows"][0]["runtimeStatus"] == "completed"
    assert "ticketDone" not in payload["rows"][0]
    assert payload["groups"] == []


def test_chat_detail_snapshot_and_patch_round_trip_without_legacy_thread_payloads() -> (
    None
):
    thread = ChatThreadProjection(
        chat_id="chat-1",
        surface="pma",
        title="Ticket chat",
        status="running",
        repo_id="repo-1",
        worktree_id="wt-1",
        ticket_id="TICKET-001",
        run_id="run-1",
        agent="hermes",
        agent_profile="m4-pma",
        chat_kind="coding_agent",
    )
    item = ChatTimelineItem(
        item_id="timeline-1",
        kind="assistant_message",
        role="assistant",
        managed_turn_id="turn-1",
        order_key="00000030|turn-1|assistant",
        section="assistant_message",
        section_order=30,
        created_at=NOW,
        text="Working on it.",
        backend_message_id="turn-1",
        identity=ChatTimelineIdentity(
            timeline_item_id="timeline-1",
            progress_item_ids=["progress-1"],
            correlation_id="client-corr-1",
        ),
        provenance=ChatTimelineProvenance(
            source_event_ids=["evt-1", "evt-2"],
            progress_event_ids=["pevt-1"],
            cursor_event_id="cursor-42",
        ),
    )
    snapshot = ChatDetailSnapshot(
        cursor=cursor(),
        thread=thread,
        timeline_window=window(),
        timeline=[item],
        queue=ChatQueueSummary(depth=1, active_turn_id="turn-1"),
        repair=repair("/hub/read-models/chats/chat-1"),
    )

    payload = dump_read_model_contract(snapshot)
    assert payload["timelineWindow"]["limit"] == 50
    assert payload["thread"]["chatKind"] == "coding_agent"
    assert payload["timeline"][0]["backendMessageId"] == "turn-1"
    assert payload["timeline"][0]["managedTurnId"] == "turn-1"
    assert payload["timeline"][0]["orderKey"] == "00000030|turn-1|assistant"
    assert payload["timeline"][0]["section"] == "assistant_message"
    assert payload["timeline"][0]["sectionOrder"] == 30
    assert payload["timeline"][0]["identity"]["timelineItemId"] == "timeline-1"
    assert payload["timeline"][0]["identity"]["progressItemIds"] == ["progress-1"]
    assert payload["timeline"][0]["identity"]["correlationId"] == "client-corr-1"
    assert payload["timeline"][0]["provenance"]["sourceEventIds"] == [
        "evt-1",
        "evt-2",
    ]
    assert payload["timeline"][0]["provenance"]["progressEventIds"] == ["pevt-1"]
    assert payload["timeline"][0]["provenance"]["cursorEventId"] == "cursor-42"
    assert load_read_model_contract(ChatDetailSnapshot, payload) == snapshot

    event = ChatDetailPatchEvent(
        envelope=envelope("chat.detail.patch", "chat", "chat-1", "upsert"),
        patch=ChatDetailPatch(appended_timeline=[item], queue=snapshot.queue),
    )
    event_payload = dump_read_model_contract(event)
    assert event_payload["patch"]["appendedTimeline"][0]["itemId"] == "timeline-1"
    assert (
        event_payload["patch"]["appendedTimeline"][0]["identity"]["timelineItemId"]
        == "timeline-1"
    )
    assert load_read_model_contract(ChatDetailPatchEvent, event_payload) == event


def test_chat_timeline_item_canonical_identity_and_provenance_fields_round_trip() -> (
    None
):
    item = ChatTimelineItem(
        item_id="tl-canonical",
        kind="tool_event",
        role="tool",
        managed_turn_id="turn-1",
        order_key="00000020|turn-1|tool",
        section="activity",
        section_order=20,
        created_at=NOW,
        text="Ran npm test",
        identity=ChatTimelineIdentity(
            timeline_item_id="tl-canonical",
            progress_item_ids=["prog-1", "prog-2"],
            correlation_id=None,
        ),
        provenance=ChatTimelineProvenance(
            source_event_ids=["src-1"],
            progress_event_ids=["pe-1", "pe-2"],
            cursor_event_id="sse-cursor-99",
        ),
    )
    payload = dump_read_model_contract(item)
    assert payload["identity"]["timelineItemId"] == "tl-canonical"
    assert payload["managedTurnId"] == "turn-1"
    assert payload["orderKey"] == "00000020|turn-1|tool"
    assert payload["section"] == "activity"
    assert payload["sectionOrder"] == 20
    assert payload["identity"]["progressItemIds"] == ["prog-1", "prog-2"]
    assert payload["provenance"]["sourceEventIds"] == ["src-1"]
    assert payload["provenance"]["cursorEventId"] == "sse-cursor-99"
    assert load_read_model_contract(ChatTimelineItem, payload) == item


def test_chat_timeline_item_rejects_legacy_identity_less_payloads() -> None:
    legacy_payload = {
        "itemId": "tl-legacy",
        "kind": "user_message",
        "role": "user",
        "createdAt": NOW.isoformat(),
        "text": "hello",
        "clientMessageId": "client-1",
        "backendMessageId": "backend-1",
    }
    with pytest.raises(ValidationError):
        load_read_model_contract(ChatTimelineItem, legacy_payload)

    repo = RepoTopology(
        repo_id="repo-1",
        label="Repo",
        path="/work/repo",
        child_worktree_ids=["wt-1"],
        worktree_setup_commands=["npm ci", "make tools"],
        is_pinned=True,
    )
    worktree = WorktreeTopology(
        worktree_id="wt-1",
        repo_id="repo-1",
        label="Feature",
        path="/work/repo-wt",
        branch="feature/read-models",
    )
    topology = RepoWorktreeTopologySnapshot(
        cursor=cursor(),
        window=window(),
        repos=[repo],
        worktrees=[worktree],
        repair=repair("/hub/read-models/repo-worktree/topology"),
    )
    runtime_row = RuntimeProjection(
        entity_kind="worktree",
        entity_id="wt-1",
        git_dirty=True,
        active_run_id="run-1",
        active_run_status="running",
        waiting_ticket_count=1,
        chat_count=1,
        updated_at=NOW,
    )
    runtime = RepoWorktreeRuntimeSnapshot(
        cursor=cursor(),
        window=window(),
        runtime=[runtime_row],
        repair=repair("/hub/read-models/repo-worktree/runtime"),
    )

    topology_payload = dump_read_model_contract(topology)
    runtime_payload = dump_read_model_contract(runtime)

    assert topology_payload["repos"][0]["childWorktreeIds"] == ["wt-1"]
    assert topology_payload["repos"][0]["isPinned"] is True
    assert topology_payload["repos"][0]["worktreeSetupCommands"] == [
        "npm ci",
        "make tools",
    ]
    assert runtime_payload["runtime"][0]["entityKind"] == "worktree"
    assert (
        load_read_model_contract(RepoWorktreeTopologySnapshot, topology_payload)
        == topology
    )
    assert (
        load_read_model_contract(RepoWorktreeRuntimeSnapshot, runtime_payload)
        == runtime
    )

    event = RepoWorktreePatchEvent(
        envelope=envelope("worktree.runtime.patch", "worktree", "wt-1", "patch"),
        patch=RepoWorktreePatch(runtime=[runtime_row]),
    )
    event_payload = dump_read_model_contract(event)
    assert event_payload["patch"]["runtime"][0]["activeRunStatus"] == "running"
    assert load_read_model_contract(RepoWorktreePatchEvent, event_payload) == event


def test_ticket_detail_snapshot_and_patch_round_trip_with_scoped_links() -> None:
    ticket = TicketProjection(
        ticket_id="tkt_1",
        route_id="TICKET-001",
        title="Define contracts",
        status="running",
        owner_kind="worktree",
        owner_id="wt-1",
        agent="codex",
    )
    sibling = TicketQueueSibling(
        ticket_id="tkt_2",
        route_id="TICKET-002",
        title="Implement projection",
        status="queued",
        previous_ticket_id="tkt_1",
    )
    snapshot = TicketDetailSnapshot(
        cursor=cursor(),
        ticket=ticket,
        siblings=[sibling],
        linked_chats=[chat_row()],
        dispatch_window=window(),
        dispatches=[{"seq": 1, "mode": "notify"}],
        repair=repair("/hub/read-models/tickets/tkt_1"),
    )

    payload = dump_read_model_contract(snapshot)
    assert payload["ticket"]["ownerKind"] == "worktree"
    assert payload["dispatchWindow"]["nextCursor"] == "projection:ui:next"
    assert load_read_model_contract(TicketDetailSnapshot, payload) == snapshot

    event = TicketDetailPatchEvent(
        envelope=envelope("ticket.detail.patch", "ticket", "tkt_1", "patch"),
        patch=TicketDetailPatch(
            ticket=ticket, siblings=[sibling], linked_chats=[chat_row()]
        ),
    )
    event_payload = dump_read_model_contract(event)
    assert event_payload["patch"]["linkedChats"][0]["ticketId"] == "TICKET-001"
    assert load_read_model_contract(TicketDetailPatchEvent, event_payload) == event
