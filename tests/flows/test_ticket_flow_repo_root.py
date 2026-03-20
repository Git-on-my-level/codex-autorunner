from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.flows.ticket_flow.definition import build_ticket_flow_definition


@pytest.fixture
def mock_agent_pool():
    pool = MagicMock()
    pool.run_turn = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_ticket_flow_resolves_repo_from_absolute_workspace_root(
    mock_agent_pool, tmp_path
):
    """When workspace_root is absolute, repo_root should be derived from it."""
    flow_def = build_ticket_flow_definition(agent_pool=mock_agent_pool)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    workspace_dir = repo_dir / "workspace"
    workspace_dir.mkdir()

    input_data: Dict[str, Any] = {
        "workspace_root": str(workspace_dir),
    }

    record = FlowRunRecord(
        id="test-id",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        current_step="ticket_turn",
        input_data=input_data,
        state={},
        created_at="2024-01-01T00:00:00Z",
    )

    with patch(
        "codex_autorunner.flows.ticket_flow.definition.find_repo_root"
    ) as mock_find:
        mock_find.side_effect = lambda start=None: start or tmp_path
        with patch(
            "codex_autorunner.flows.ticket_flow.definition.TicketRunner"
        ) as mock_runner_class:
            mock_runner = AsyncMock()
            mock_runner.step = AsyncMock(
                return_value=MagicMock(status="running", state={})
            )
            mock_runner_class.return_value = mock_runner

            await flow_def.steps["ticket_turn"](record, input_data, None)

            mock_runner_class.assert_called_once()
            call_kwargs = mock_runner_class.call_args[1]
            assert call_kwargs["workspace_root"] == workspace_dir


@pytest.mark.asyncio
async def test_ticket_flow_rejects_relative_workspace_root(mock_agent_pool):
    """Persisted ticket_flow runs must carry an absolute workspace_root."""
    flow_def = build_ticket_flow_definition(agent_pool=mock_agent_pool)

    input_data: Dict[str, Any] = {
        "workspace_root": "workspace",
    }

    record = FlowRunRecord(
        id="test-id",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        current_step="ticket_turn",
        input_data=input_data,
        state={},
        created_at="2024-01-01T00:00:00Z",
    )

    with pytest.raises(ValueError, match="workspace_root must be absolute"):
        await flow_def.steps["ticket_turn"](record, input_data, None)


@pytest.mark.asyncio
async def test_ticket_flow_fallback_to_cwd_when_no_workspace_root(
    mock_agent_pool, tmp_path
):
    """When workspace_root is not provided, fall back to find_repo_root() from CWD."""
    flow_def = build_ticket_flow_definition(agent_pool=mock_agent_pool)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    input_data: Dict[str, Any] = {}

    record = FlowRunRecord(
        id="test-id",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        current_step="ticket_turn",
        input_data=input_data,
        state={},
        created_at="2024-01-01T00:00:00Z",
    )

    with patch(
        "codex_autorunner.flows.ticket_flow.definition.find_repo_root"
    ) as mock_find:
        mock_find.return_value = repo_dir
        with patch(
            "codex_autorunner.flows.ticket_flow.definition.TicketRunner"
        ) as mock_runner_class:
            mock_runner = AsyncMock()
            mock_runner.step = AsyncMock(
                return_value=MagicMock(status="running", state={})
            )
            mock_runner_class.return_value = mock_runner

            await flow_def.steps["ticket_turn"](record, input_data, None)

            mock_runner_class.assert_called_once()
            call_kwargs = mock_runner_class.call_args[1]
            assert call_kwargs["workspace_root"] == repo_dir


def test_ticket_flow_schema_only_exposes_workspace_and_max_turns(
    mock_agent_pool,
):
    flow_def = build_ticket_flow_definition(agent_pool=mock_agent_pool)

    properties = flow_def.input_schema["properties"]

    assert set(properties) == {"workspace_root", "max_total_turns"}


@pytest.mark.asyncio
async def test_ticket_flow_uses_definition_defaults_for_runner_config(
    mock_agent_pool, tmp_path
):
    flow_def = build_ticket_flow_definition(
        agent_pool=mock_agent_pool,
        auto_commit_default=True,
        include_previous_ticket_context_default=True,
    )
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    input_data: Dict[str, Any] = {}
    record = FlowRunRecord(
        id="test-id",
        flow_type="ticket_flow",
        status=FlowRunStatus.RUNNING,
        current_step="ticket_turn",
        input_data=input_data,
        state={},
        created_at="2024-01-01T00:00:00Z",
    )

    with patch(
        "codex_autorunner.flows.ticket_flow.definition.find_repo_root"
    ) as mock_find:
        mock_find.return_value = repo_dir
        with patch(
            "codex_autorunner.flows.ticket_flow.definition.TicketRunner"
        ) as mock_runner_class:
            mock_runner = AsyncMock()
            mock_runner.step = AsyncMock(
                return_value=MagicMock(status="running", state={})
            )
            mock_runner_class.return_value = mock_runner

            await flow_def.steps["ticket_turn"](record, input_data, None)

            config = mock_runner_class.call_args.kwargs["config"]
            assert config.auto_commit is True
            assert config.include_previous_ticket_context is True
