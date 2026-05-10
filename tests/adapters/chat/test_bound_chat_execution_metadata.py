from __future__ import annotations

from codex_autorunner.adapters.chat.bound_chat_execution_metadata import (
    bound_chat_execution_origin,
    bound_chat_execution_progress_targets,
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


def test_web_discord_and_telegram_are_first_class_progress_surfaces() -> None:
    merged = merge_bound_chat_execution_metadata(
        {},
        origin_kind="surface",
        origin_surface_kind="web",
        origin_surface_key="managed-thread-1",
        progress_targets=(
            ("web", "managed-thread-1"),
            ("discord", "channel-1"),
            ("telegram", "chat-1:55"),
            ("web", "managed-thread-1"),
        ),
    )

    assert bound_chat_execution_origin(merged) == (
        "surface",
        "web",
        "managed-thread-1",
    )
    assert bound_chat_execution_progress_targets(merged) == (
        ("web", "managed-thread-1"),
        ("discord", "channel-1"),
        ("telegram", "chat-1:55"),
    )


def test_legacy_pma_web_surface_kind_normalizes_to_web() -> None:
    merged = merge_bound_chat_execution_metadata(
        {},
        origin_kind="surface",
        origin_surface_kind="pma_web",
        origin_surface_key="managed-thread-1",
        progress_targets=(("pma_web", "managed-thread-1"),),
    )

    assert merged["bound_chat_execution"] == {
        "origin": {
            "kind": "surface",
            "surface_kind": "web",
            "surface_key": "managed-thread-1",
        },
        "progress_targets": [
            {"surface_kind": "web", "surface_key": "managed-thread-1"}
        ],
    }
