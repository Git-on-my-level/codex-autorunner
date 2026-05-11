from __future__ import annotations

from datetime import datetime, timezone

from codex_autorunner.surfaces.web.read_model_contracts import (
    ChatDetailPatch,
    ChatDetailPatchEvent,
    ChatDetailSnapshot,
    ChatIndexCounters,
    ChatIndexPatch,
    ChatIndexPatchEvent,
    ChatIndexRow,
    ChatIndexSnapshot,
    ChatQueueSummary,
    ChatThreadProjection,
    ChatTimelineItem,
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
        status="running",
        unread_count=2,
        last_activity_at=NOW,
        repo_id="repo-1",
        worktree_id="wt-1",
        ticket_id="TICKET-001",
        run_id="run-1",
        agent="codex",
        model="gpt-5.3-codex",
        group_id="ticket-run:run-1",
    )


def test_chat_index_snapshot_and_patch_round_trip_with_camel_case_payloads() -> None:
    snapshot = ChatIndexSnapshot(
        cursor=cursor(),
        window=window(),
        filter="active",
        rows=[chat_row()],
        counters=ChatIndexCounters(total=1, waiting=0, running=1, unread=2, archived=0),
        repair=repair("/hub/read-models/chats"),
    )
    payload = dump_read_model_contract(snapshot)

    assert payload["contractVersion"] == "web-read-models.v1"
    assert payload["rows"][0]["chatId"] == "chat-1"
    assert load_read_model_contract(ChatIndexSnapshot, payload) == snapshot

    event = ChatIndexPatchEvent(
        envelope=envelope("chat.index.patch", "chat", "chat-1", "patch"),
        patch=ChatIndexPatch(rows=[chat_row()], counters=snapshot.counters),
    )
    event_payload = dump_read_model_contract(event)

    assert event_payload["envelope"]["eventType"] == "chat.index.patch"
    assert event_payload["patch"]["rows"][0]["unreadCount"] == 2
    assert load_read_model_contract(ChatIndexPatchEvent, event_payload) == event


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
    )
    item = ChatTimelineItem(
        item_id="timeline-1",
        kind="assistant_message",
        role="assistant",
        created_at=NOW,
        text="Working on it.",
        backend_message_id="turn-1",
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
    assert payload["timeline"][0]["backendMessageId"] == "turn-1"
    assert load_read_model_contract(ChatDetailSnapshot, payload) == snapshot

    event = ChatDetailPatchEvent(
        envelope=envelope("chat.detail.patch", "chat", "chat-1", "upsert"),
        patch=ChatDetailPatch(appended_timeline=[item], queue=snapshot.queue),
    )
    event_payload = dump_read_model_contract(event)
    assert event_payload["patch"]["appendedTimeline"][0]["itemId"] == "timeline-1"
    assert load_read_model_contract(ChatDetailPatchEvent, event_payload) == event


def test_repo_worktree_topology_and_runtime_contracts_round_trip_separately() -> None:
    repo = RepoTopology(
        repo_id="repo-1",
        label="Repo",
        path="/work/repo",
        child_worktree_ids=["wt-1"],
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
