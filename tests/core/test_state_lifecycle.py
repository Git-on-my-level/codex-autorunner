from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.state_lifecycle import (
    DEFAULT_STATE_LIFECYCLE_CONTROLLER,
    LifecycleAction,
    LifecycleArchiveSpec,
    LifecycleReason,
    lifecycle_action_for_cleanup_action,
    lifecycle_reason_for_cleanup_reason,
)
from codex_autorunner.core.state_retention import CleanupAction, CleanupReason


def test_plan_archive_transitions_filters_by_intent_and_path() -> None:
    transitions = DEFAULT_STATE_LIFECYCLE_CONTROLLER.plan_archive_transitions(
        specs=(
            LifecycleArchiveSpec(
                family="tickets",
                key="tickets",
                archive_dest="tickets",
                archive_intents=frozenset({"review"}),
                reason=LifecycleReason.REVIEW_ARTIFACT,
            ),
            LifecycleArchiveSpec(
                family="logs",
                key="codex-autorunner.log",
                archive_dest="logs/codex-autorunner.log",
                archive_intents=frozenset({"full"}),
                reason=LifecycleReason.COLD_TRACE,
            ),
        ),
        source_root=Path("/repo/.codex-autorunner"),
        dest_root=Path("/repo/.codex-autorunner/archive/worktrees/snap"),
        intent="review",
        path_filter=("tickets",),
    )

    assert len(transitions) == 1
    assert transitions[0].key == "tickets"
    assert transitions[0].decision.family == "tickets"
    assert transitions[0].decision.action == LifecycleAction.ARCHIVE
    assert transitions[0].decision.reason == LifecycleReason.REVIEW_ARTIFACT


def test_summarize_decisions_groups_actions_and_families() -> None:
    summary = DEFAULT_STATE_LIFECYCLE_CONTROLLER.summarize_decisions(
        [
            transition.decision
            for transition in DEFAULT_STATE_LIFECYCLE_CONTROLLER.plan_archive_transitions(
                specs=(
                    LifecycleArchiveSpec(
                        family="tickets",
                        key="tickets",
                        archive_dest="tickets",
                        archive_intents=frozenset({"review"}),
                        reason=LifecycleReason.REVIEW_ARTIFACT,
                    ),
                    LifecycleArchiveSpec(
                        family="github_context",
                        key="github_context",
                        archive_dest="github_context",
                        archive_intents=frozenset({"review"}),
                        reason=LifecycleReason.REVIEW_ARTIFACT,
                        required=False,
                    ),
                ),
                source_root=Path("/repo/.codex-autorunner"),
                dest_root=Path("/repo/.codex-autorunner/archive/worktrees/snap"),
                intent="review",
            )
        ]
    )

    assert summary["total"] == 2
    assert summary["actions"] == {"archive": 2}
    assert summary["families"]["tickets"]["actions"] == {"archive": 1}
    assert summary["reasons"] == {"review_artifact": 2}


def test_cleanup_action_and_reason_map_to_lifecycle_vocabulary() -> None:
    assert (
        lifecycle_action_for_cleanup_action(CleanupAction.KEEP) == LifecycleAction.KEEP
    )
    assert (
        lifecycle_action_for_cleanup_action(CleanupAction.ARCHIVE_THEN_PRUNE)
        == LifecycleAction.ARCHIVE_THEN_PRUNE
    )
    assert (
        lifecycle_reason_for_cleanup_reason(CleanupReason.ACTIVE_RUN_GUARD)
        == LifecycleReason.ACTIVE_RUN_GUARD
    )
    assert (
        lifecycle_reason_for_cleanup_reason(CleanupReason.CACHE_REBUILDABLE)
        == LifecycleReason.CACHE_REBUILDABLE
    )
