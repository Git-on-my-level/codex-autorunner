from __future__ import annotations

import pytest

from codex_autorunner.core.orchestration.ticket_flow_chat_ledger_contract import (
    TICKET_FLOW_CHAT_LEDGER_CONTRACT_VERSION,
    TICKET_FLOW_LEDGER_LIFECYCLE,
    TICKET_FLOW_LEDGER_RECORDS,
    TicketFlowThreadLink,
    ticket_flow_chat_ledger_contract,
    ticket_flow_thread_link_key,
    ticket_flow_thread_metadata,
    validate_ticket_flow_thread_metadata,
)


def test_ticket_flow_chat_ledger_contract_lists_required_lifecycle_facts() -> None:
    assert TICKET_FLOW_CHAT_LEDGER_CONTRACT_VERSION == "ticket_flow_chat_ledger.v1"
    assert set(TICKET_FLOW_LEDGER_LIFECYCLE) >= {
        "flow_run.started",
        "flow_run.completed",
        "flow_run.failed",
        "ticket.selected",
        "managed_thread.created",
        "managed_thread.reused",
        "ticket_thread.linked",
        "ticket_turn.started",
        "ticket_turn.completed",
        "ticket_turn.failed",
    }


def test_ticket_flow_chat_ledger_contract_separates_flows_db_from_orchestration() -> (
    None
):
    contract = ticket_flow_chat_ledger_contract()
    records = {record["name"]: record for record in contract["records"]}

    assert records["repo-local flow run state"]["source"] == "flows.db"
    assert (
        "state.ticket_engine" in records["repo-local flow run state"]["required_fields"]
    )

    orchestration_records = [
        record for record in contract["records"] if record["source"] == "orchestration"
    ]
    assert orchestration_records
    assert any(
        record["artifact"] == "thread_link"
        and {
            "flow_run_id",
            "ticket_id",
            "managed_thread_id",
            "workspace_root",
        }.issubset(set(record["required_fields"]))
        for record in orchestration_records
    )
    assert "flows.db" in contract["read_models"]["future_flow_index"]
    assert (
        "must not read repo-local flows.db"
        in contract["read_models"]["web_hub_chat_index"]
    )


def test_ticket_flow_thread_metadata_matches_chat_index_contract_shape() -> None:
    metadata = ticket_flow_thread_metadata(
        flow_run_id="run-123",
        ticket_id="TICKET-002",
        workspace_root="/repo/worktree",
        repo_id="repo",
        worktree_id="repo--discord-2",
        ticket_path=".codex-autorunner/tickets/TICKET-002.md",
        extra={"agent_profile": "codex"},
    )

    assert metadata == {
        "contract_version": "ticket_flow_chat_ledger.v1",
        "flow_type": "ticket_flow",
        "thread_kind": "ticket_flow",
        "run_id": "run-123",
        "flow_run_id": "run-123",
        "ticket_id": "TICKET-002",
        "workspace_root": "/repo/worktree",
        "ticket_flow_link_key": "ticket_flow:run-123:TICKET-002",
        "repo_id": "repo",
        "worktree_id": "repo--discord-2",
        "ticket_path": ".codex-autorunner/tickets/TICKET-002.md",
        "agent_profile": "codex",
    }
    validate_ticket_flow_thread_metadata(metadata)


def test_ticket_flow_thread_link_is_repairable_from_flow_run_and_ticket() -> None:
    link = TicketFlowThreadLink(
        flow_run_id="run-123",
        ticket_id="TICKET-002",
        managed_thread_id="thread-abc",
        workspace_root="/repo/worktree",
        repo_id="repo",
    )

    assert link.link_key == "ticket_flow:run-123:TICKET-002"
    metadata = link.to_thread_metadata()
    assert metadata["ticket_flow_link_key"] == link.link_key
    assert metadata["run_id"] == "run-123"
    assert metadata["flow_run_id"] == "run-123"
    assert metadata["ticket_id"] == "TICKET-002"


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"flow_type": "ticket_flow", "thread_kind": "ticket_flow"},
        {
            "flow_type": "ticket_flow",
            "thread_kind": "ticket_flow",
            "run_id": "run-1",
            "flow_run_id": "run-2",
            "ticket_id": "TICKET-002",
            "workspace_root": "/repo",
        },
    ],
)
def test_ticket_flow_thread_metadata_validation_rejects_missing_or_split_links(
    metadata: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        validate_ticket_flow_thread_metadata(metadata)


def test_ticket_flow_thread_link_key_requires_stable_identity() -> None:
    with pytest.raises(ValueError):
        ticket_flow_thread_link_key("", "TICKET-002")

    with pytest.raises(ValueError):
        ticket_flow_thread_link_key("run-123", None)


def test_ticket_flow_ledger_records_are_serializable_and_named() -> None:
    assert all(record.name for record in TICKET_FLOW_LEDGER_RECORDS)
    assert all(record.required_fields for record in TICKET_FLOW_LEDGER_RECORDS)
    assert ticket_flow_chat_ledger_contract()["records"] == [
        record.to_dict() for record in TICKET_FLOW_LEDGER_RECORDS
    ]
