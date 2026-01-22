import logging
from typing import Optional, Tuple

from ...core.flows import (
    FlowDefinition,
    StepOutcome,
)
from ...core.git_utils import GitError, git_branch, git_is_clean, run_git
from ...core.utils import find_repo_root
from .models import PrFlowInput, PrFlowState, TargetType

_logger = logging.getLogger(__name__)


async def preflight_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Preflight check for run %s", record.id)
    repo_root = find_repo_root()

    errors = []

    try:
        is_clean = git_is_clean(repo_root)
        if not is_clean:
            errors.append("Working directory not clean (uncommitted changes)")
    except Exception as e:
        _logger.warning("Failed to check git cleanliness: %s", e)

    if errors:
        return StepOutcome.fail("Preflight failed: " + "; ".join(errors))

    return StepOutcome.continue_to(
        next_steps={"resolve_target"},
        output={"preflight_complete": True},
    )


async def resolve_target_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Resolving target for run %s", record.id)

    flow_input = PrFlowInput(**input_data)
    state = PrFlowState(**record.state)

    state.target_type = flow_input.input_type

    if flow_input.input_type == TargetType.ISSUE and flow_input.issue_url:
        state.target_url = flow_input.issue_url
        owner, repo, issue_number = _parse_issue_url(flow_input.issue_url)
        if owner and repo and issue_number:
            state.owner = owner
            state.repo = repo
            state.issue_number = issue_number
        else:
            return StepOutcome.fail(f"Invalid issue URL: {flow_input.issue_url}")
    elif flow_input.input_type == TargetType.PR and flow_input.pr_url:
        state.target_url = flow_input.pr_url
        owner, repo, pr_number = _parse_pr_url(flow_input.pr_url)
        if owner and repo and pr_number:
            state.owner = owner
            state.repo = repo
            state.pr_number = pr_number
        else:
            return StepOutcome.fail(f"Invalid PR URL: {flow_input.pr_url}")
    else:
        return StepOutcome.fail("Invalid target configuration")

    return StepOutcome.continue_to(
        next_steps={"prepare_workspace"},
        output=state.model_dump(),
    )


async def prepare_workspace_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Preparing workspace for run %s", record.id)

    repo_root = find_repo_root()
    worktree_root = repo_root / ".codex-autorunner" / "worktrees"
    worktree_path = worktree_root / record.id

    state = PrFlowState(**record.state)

    try:
        worktree_root.mkdir(parents=True, exist_ok=True)

        current_branch = git_branch(repo_root)
        if not current_branch:
            return StepOutcome.fail("Failed to get current branch")

        state.branch = current_branch

        if worktree_path.exists():
            _logger.info("Worktree already exists, reusing: %s", worktree_path)
        else:
            # Create worktree from HEAD (current commit) to avoid branch-in-use errors
            run_git(["worktree", "add", str(worktree_path), "HEAD"], repo_root)
            _logger.info("Created worktree: %s", worktree_path)

        state.workspace_path = str(worktree_path)
    except GitError as e:
        return StepOutcome.fail(f"Failed to prepare workspace: {e}")
    except Exception as e:
        _logger.exception("Unexpected error preparing workspace")
        return StepOutcome.fail(f"Workspace preparation failed: {e}")

    return StepOutcome.continue_to(
        next_steps={"link_issue_or_pr"},
        output=state.model_dump(),
    )


async def link_issue_or_pr_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Linking issue/PR for run %s", record.id)

    repo_root = find_repo_root()
    worktree_root = repo_root / ".codex-autorunner" / "worktrees"
    worktree_path = worktree_root / record.id

    state = PrFlowState(**record.state)

    try:
        if state.target_type == TargetType.ISSUE:
            branch_name = f"pr-flow/{record.id}"
            run_git(["checkout", "-b", branch_name], worktree_path)
            state.branch = branch_name
            _logger.info("Created branch for issue: %s", branch_name)
        elif state.target_type == TargetType.PR:
            fetch_ref = f"pull/{state.pr_number}/head"
            local_branch = f"pr-{state.pr_number}"
            run_git(
                ["fetch", "origin", f"{fetch_ref}:{local_branch}"],
                worktree_path,
            )
            run_git(["checkout", local_branch], worktree_path)
            state.branch = local_branch
            _logger.info("Checked out PR branch: %s", state.branch)
        else:
            return StepOutcome.fail("No target type in state")
    except GitError as e:
        return StepOutcome.fail(f"Failed to create/checkout branch: {e}")

    return StepOutcome.continue_to(
        next_steps={"generate_spec"},
        output=state.model_dump(),
    )


async def generate_spec_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Generating spec for run %s", record.id)

    return StepOutcome.continue_to(
        next_steps={"implement_cycle"},
        output={"spec_generated": True},
    )


async def implement_cycle_step(record, input_data: dict) -> StepOutcome:
    _logger.info(
        "Implementing cycle %d for run %s",
        record.state.get("cycle_count", 0),
        record.id,
    )

    cycle_count = record.state.get("cycle_count", 0) + 1

    if cycle_count >= 3:
        return StepOutcome.continue_to(
            next_steps={"sync_pr"},
            output={"cycle_count": cycle_count},
        )
    else:
        return StepOutcome.continue_to(
            next_steps={"implement_cycle"},
            output={"cycle_count": cycle_count},
        )


async def sync_pr_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Syncing PR for run %s", record.id)

    return StepOutcome.continue_to(
        next_steps={"wait_for_feedback"},
        output={"synced": True},
    )


async def wait_for_feedback_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Waiting for feedback for run %s", record.id)

    feedback_count = record.state.get("feedback_count", 0)
    if feedback_count < 2:
        return StepOutcome.continue_to(
            next_steps={"apply_feedback"},
            output={"feedback_count": feedback_count + 1},
        )
    else:
        return StepOutcome.continue_to(
            next_steps={"finalize"},
            output={"feedback_count": feedback_count},
        )


async def apply_feedback_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Applying feedback for run %s", record.id)

    return StepOutcome.continue_to(
        next_steps={"implement_cycle"},
        output={"feedback_applied": True},
    )


async def finalize_step(record, input_data: dict) -> StepOutcome:
    _logger.info("Finalizing run %s", record.id)

    return StepOutcome.complete(
        output={
            "finalized": True,
            "final_report": "PR flow completed (placeholder implementation; no PR actions executed).",
        }
    )


def _parse_issue_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    import re

    # Match github.com issue URLs, ensuring github.com is a proper domain
    pattern = (
        r"^(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/]+)/issues/(\d+)(?:/.*)?$"
    )
    match = re.match(pattern, url)
    if match:
        return match.group(1), match.group(2), int(match.group(3))
    return None, None, None


def _parse_pr_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    import re

    # Match github.com PR URLs, ensuring github.com is a proper domain
    pattern = (
        r"^(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:/.*)?$"
    )
    match = re.match(pattern, url)
    if match:
        return match.group(1), match.group(2), int(match.group(3))
    return None, None, None


def build_pr_flow_definition() -> FlowDefinition:
    steps = {
        "preflight": preflight_step,
        "resolve_target": resolve_target_step,
        "prepare_workspace": prepare_workspace_step,
        "link_issue_or_pr": link_issue_or_pr_step,
        "generate_spec": generate_spec_step,
        "implement_cycle": implement_cycle_step,
        "sync_pr": sync_pr_step,
        "wait_for_feedback": wait_for_feedback_step,
        "apply_feedback": apply_feedback_step,
        "finalize": finalize_step,
    }

    definition = FlowDefinition(
        flow_type="pr_flow",
        initial_step="preflight",
        steps=steps,
    )

    definition.validate()

    return definition
