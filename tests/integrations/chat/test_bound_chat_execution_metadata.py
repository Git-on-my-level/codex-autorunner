from __future__ import annotations

from codex_autorunner.integrations.chat.bound_chat_execution_metadata import (
    execution_mapping_has_chat_surface_origin,
    merge_bound_chat_execution_metadata,
)


def test_github_scm_wake_metadata_carries_progress_without_surface_origin() -> None:
    merged = merge_bound_chat_execution_metadata(
        {},
        origin_kind="github_scm",
        progress_targets=(("discord", "channel-1"),),
    )
    assert merged["bound_chat_execution"]["origin"] == {"kind": "github_scm"}
    assert merged["bound_chat_execution"]["progress_targets"] == [
        {"surface_kind": "discord", "surface_key": "channel-1"}
    ]


def test_execution_mapping_does_not_treat_github_scm_as_chat_surface_origin() -> None:
    execution = {
        "metadata": merge_bound_chat_execution_metadata(
            {},
            origin_kind="github_scm",
            progress_targets=(("discord", "ch-1"),),
        ),
    }
    assert execution_mapping_has_chat_surface_origin(execution) is False
