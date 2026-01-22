"""
PR Flow step idempotency tests.

Tests verify that steps can be safely re-run after crash/resume without
causing corruption or inconsistency.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codex_autorunner.core.flows import (
    FlowController,
    FlowRunRecord,
    FlowRunStatus,
)
from codex_autorunner.flows.pr_flow import (
    PrFlowInput,
    TargetType,
    build_pr_flow_definition,
)
from codex_autorunner.flows.pr_flow.definition import (
    _parse_issue_url,
    _parse_pr_url,
    link_issue_or_pr_step,
    preflight_step,
    prepare_workspace_step,
    resolve_target_step,
)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def git_repo(temp_dir):
    """Create a temporary git repository."""
    repo_root = temp_dir / "repo"
    repo_root.mkdir()

    # Initialize git repo
    import subprocess

    subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    # Create initial commit
    (repo_root / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )

    return repo_root


@pytest.fixture
def flow_controller(temp_dir):
    """Create a flow controller with PR flow definition."""
    definition = build_pr_flow_definition()
    db_path = temp_dir / "flow.db"
    artifacts_root = temp_dir / "artifacts"
    controller = FlowController(
        definition=definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()
    yield controller
    controller.shutdown()


@pytest.fixture
def mock_record():
    """Create a mock flow run record."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run-001"
    record.flow_type = "pr_flow"
    record.status = FlowRunStatus.RUNNING
    record.state = {}
    record.input_data = {}
    return record


def test_parse_issue_url():
    """Test issue URL parsing."""
    owner, repo, issue_number = _parse_issue_url(
        "https://github.com/owner/repo/issues/42"
    )
    assert owner == "owner"
    assert repo == "repo"
    assert issue_number == 42

    owner, repo, issue_number = _parse_issue_url("invalid-url")
    assert owner is None
    assert repo is None
    assert issue_number is None


def test_parse_pr_url():
    """Test PR URL parsing."""
    owner, repo, pr_number = _parse_pr_url("https://github.com/owner/repo/pull/123")
    assert owner == "owner"
    assert repo == "repo"
    assert pr_number == 123

    owner, repo, pr_number = _parse_pr_url("invalid-url")
    assert owner is None
    assert repo is None
    assert pr_number is None


@pytest.mark.asyncio
async def test_preflight_step_clean_repo(git_repo):
    """Test preflight step passes on clean repository."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"

    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        outcome = await preflight_step(record, {})

        assert outcome.status == FlowRunStatus.RUNNING
        assert "preflight_complete" in outcome.output


@pytest.mark.asyncio
async def test_preflight_step_dirty_repo(git_repo):
    """Test preflight step fails on dirty repository."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"

    # Create uncommitted changes
    (git_repo / "dirty.md").write_text("# Uncommitted\n")

    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        outcome = await preflight_step(record, {})

        assert outcome.status == FlowRunStatus.FAILED
        assert outcome.error is not None
        assert "not clean" in outcome.error


@pytest.mark.asyncio
async def test_resolve_target_step_issue():
    """Test target resolution for issue URL."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"
    record.state = {}

    input_data = {
        "input_type": "issue",
        "issue_url": "https://github.com/owner/repo/issues/42",
    }

    outcome = await resolve_target_step(record, input_data)

    assert outcome.status == FlowRunStatus.RUNNING
    assert "target_type" in outcome.output
    assert "owner" in outcome.output
    assert "repo" in outcome.output
    assert "issue_number" in outcome.output
    assert outcome.output["owner"] == "owner"
    assert outcome.output["repo"] == "repo"
    assert outcome.output["issue_number"] == 42


@pytest.mark.asyncio
async def test_resolve_target_step_pr():
    """Test target resolution for PR URL."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"
    record.state = {}

    input_data = {
        "input_type": "pr",
        "pr_url": "https://github.com/owner/repo/pull/123",
    }

    outcome = await resolve_target_step(record, input_data)

    assert outcome.status == FlowRunStatus.RUNNING
    assert "target_type" in outcome.output
    assert "owner" in outcome.output
    assert "repo" in outcome.output
    assert "pr_number" in outcome.output
    assert outcome.output["owner"] == "owner"
    assert outcome.output["repo"] == "repo"
    assert outcome.output["pr_number"] == 123


@pytest.mark.asyncio
async def test_resolve_target_step_invalid_url():
    """Test target resolution fails on invalid URL."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"
    record.state = {}

    input_data = {
        "input_type": "issue",
        "issue_url": "https://not-github.com/owner/repo/issues/42",
    }

    outcome = await resolve_target_step(record, input_data)

    assert outcome.status == FlowRunStatus.FAILED


@pytest.mark.asyncio
async def test_prepare_workspace_step_creates_worktree(git_repo, temp_dir):
    """Test workspace preparation creates new worktree."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"
    record.state = {"branch": "main"}

    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        outcome = await prepare_workspace_step(record, {})

        assert outcome.status == FlowRunStatus.RUNNING
        assert "workspace_path" in outcome.output

        worktree_path = Path(outcome.output["workspace_path"])
        assert worktree_path.exists()
        assert worktree_path.is_dir()


@pytest.mark.asyncio
async def test_prepare_workspace_step_reuses_existing_worktree(git_repo, temp_dir):
    """Test workspace preparation reuses existing worktree (idempotency)."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"
    record.state = {}

    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        # First run - creates worktree
        outcome1 = await prepare_workspace_step(record, {})
        assert outcome1.status == FlowRunStatus.RUNNING

        worktree_path = Path(outcome1.output["workspace_path"])
        assert worktree_path.exists()

        # Second run - should reuse existing worktree
        record.state = {"branch": "main"}
        outcome2 = await prepare_workspace_step(record, {})
        assert outcome2.status == FlowRunStatus.RUNNING
        assert outcome2.output["workspace_path"] == outcome1.output["workspace_path"]


@pytest.mark.skip(reason="Git environment-dependent test")
@pytest.mark.asyncio
async def test_link_issue_step_creates_branch(git_repo, temp_dir):
    """Test linking issue creates new branch."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"

    # First, create a worktree (this is what would happen in real flow)
    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        ws_outcome = await prepare_workspace_step(record, {})
        assert ws_outcome.status == FlowRunStatus.RUNNING

    record.state = {
        "target_type": TargetType.ISSUE,
        "workspace_path": ws_outcome.output["workspace_path"],
    }

    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        outcome = await link_issue_or_pr_step(record, {})

        assert outcome.status == FlowRunStatus.RUNNING
        assert "branch" in outcome.output
        assert "pr-flow/test-run" in outcome.output["branch"]


@pytest.mark.skip(reason="Git environment-dependent test")
@pytest.mark.asyncio
async def test_link_pr_step_checks_out_branch(git_repo, temp_dir):
    """Test linking PR checks out PR branch."""
    record = MagicMock(spec=FlowRunRecord)
    record.id = "test-run"

    # Create a PR branch in the repo
    import subprocess

    subprocess.run(
        ["git", "checkout", "-b", "pr-123"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    # Create worktree
    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        ws_outcome = await prepare_workspace_step(record, {})
        assert ws_outcome.status == FlowRunStatus.RUNNING

    record.state = {
        "target_type": TargetType.PR,
        "pr_number": 123,
        "workspace_path": ws_outcome.output["workspace_path"],
    }

    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        outcome = await link_issue_or_pr_step(record, {})

        assert outcome.status == FlowRunStatus.RUNNING
        assert "branch" in outcome.output
        assert "pr-123" in outcome.output["branch"]


@pytest.mark.asyncio
async def test_pr_flow_resume_from_prepare_workspace(git_repo, temp_dir):
    """Test resuming flow from prepare_workspace step (worktree reuse)."""
    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        definition = build_pr_flow_definition()
        db_path = temp_dir / "resume-test.db"
        artifacts_root = temp_dir / "resume-artifacts"

        controller = FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
        )
        controller.initialize()

        try:
            # Start flow with issue input
            input_data = PrFlowInput(
                input_type=TargetType.ISSUE,
                issue_url="https://github.com/owner/repo/issues/42",
            ).model_dump()

            record = await controller.start_flow(input_data=input_data)
            runner = asyncio.create_task(controller.run_flow(record.id))
            await asyncio.sleep(0.2)
            await controller.stop_flow(record.id)
            await runner

            status = controller.get_status(record.id)
            assert status is not None

            if status.status == FlowRunStatus.STOPPED:
                await controller.resume_flow(record.id)
                final_status = await controller.run_flow(record.id)
                assert final_status.status in {
                    FlowRunStatus.COMPLETED,
                    FlowRunStatus.STOPPED,
                }

        finally:
            controller.shutdown()


@pytest.mark.asyncio
async def test_pr_flow_resume_from_link_issue_or_pr(git_repo, temp_dir):
    """Test resuming flow from link_issue_or_pr step (branch reuse)."""
    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        definition = build_pr_flow_definition()
        db_path = temp_dir / "branch-reuse-test.db"
        artifacts_root = temp_dir / "branch-reuse-artifacts"

        controller = FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
        )
        controller.initialize()

        try:
            # Start flow
            input_data = PrFlowInput(
                input_type=TargetType.ISSUE,
                issue_url="https://github.com/owner/repo/issues/42",
            ).model_dump()

            record = await controller.start_flow(input_data=input_data)
            runner = asyncio.create_task(controller.run_flow(record.id))
            await asyncio.sleep(0.2)
            await controller.stop_flow(record.id)
            await runner

            status = controller.get_status(record.id)
            if status.status == FlowRunStatus.STOPPED:
                await controller.resume_flow(record.id)
                final_status = await controller.run_flow(record.id)
                resumed_branch = final_status.state.get("branch")
                assert resumed_branch is not None

        finally:
            controller.shutdown()


@pytest.mark.asyncio
async def test_pr_flow_complete_run_with_valid_issue(git_repo, temp_dir):
    """Test complete PR flow run with valid issue input."""
    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        definition = build_pr_flow_definition()
        db_path = temp_dir / "complete-test.db"
        artifacts_root = temp_dir / "complete-artifacts"

        controller = FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
        )
        controller.initialize()

        try:
            input_data = PrFlowInput(
                input_type=TargetType.ISSUE,
                issue_url="https://github.com/owner/repo/issues/42",
            ).model_dump()

            record = await controller.start_flow(input_data=input_data)
            final_status = await controller.run_flow(record.id)
            assert final_status.status != FlowRunStatus.FAILED

        finally:
            controller.shutdown()


@pytest.mark.asyncio
async def test_pr_flow_invalid_input_fails(git_repo, temp_dir):
    """Test PR flow fails gracefully on invalid input."""
    with patch(
        "codex_autorunner.flows.pr_flow.definition.find_repo_root",
        return_value=git_repo,
    ):
        definition = build_pr_flow_definition()
        db_path = temp_dir / "invalid-input-test.db"
        artifacts_root = temp_dir / "invalid-input-artifacts"

        controller = FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
        )
        controller.initialize()

        try:
            # Invalid URL should cause failure
            input_data = {
                "input_type": "issue",
                "issue_url": "https://not-github.com/owner/repo/issues/42",
            }

            record = await controller.start_flow(input_data=input_data)
            final_status = await controller.run_flow(record.id)
            assert final_status.status == FlowRunStatus.FAILED

        finally:
            controller.shutdown()
