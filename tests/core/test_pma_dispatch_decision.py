from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.pma_dispatch_decision import build_pma_dispatch_decision


def test_build_pma_dispatch_decision_accepts_origin_thread_delivery_target(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo-a"

    decision = build_pma_dispatch_decision(
        message="Terminal follow-up",
        requested_delivery="auto",
        source_kind="managed_thread_completed",
        repo_id="repo-a",
        workspace_root=workspace,
        managed_thread_id="watched-thread",
        delivery_target={
            "surface_kind": "discord",
            "surface_key": "origin-discord",
        },
        context_payload={
            "wake_up": {
                "metadata": {
                    "pma_origin": {
                        "thread_id": "origin-thread",
                    }
                }
            }
        },
        binding_metadata_by_thread={
            "origin-thread": {
                "binding_kind": "discord",
                "binding_id": "origin-discord",
            }
        },
        preferred_bound_surface_kinds=("discord", "telegram"),
    )

    assert decision.suppress_publish is False
    assert [attempt.route for attempt in decision.attempts] == [
        "explicit",
        "primary_pma",
        "primary_pma",
        "bound",
        "bound",
    ]
    assert decision.attempts[0].surface_kind == "discord"
    assert decision.attempts[0].surface_key == "origin-discord"


def test_build_pma_dispatch_decision_suppresses_duplicate_only_for_managed_thread_match() -> (
    None
):
    decision = build_pma_dispatch_decision(
        message="Already handled. No action needed.",
        requested_delivery="auto",
        source_kind="managed_thread_completed",
        repo_id="repo-a",
        workspace_root=None,
        managed_thread_id="watched-thread",
        delivery_target={
            "surface_kind": "discord",
            "surface_key": "watched-discord",
        },
        context_payload=None,
        binding_metadata_by_thread={
            "watched-thread": {
                "binding_kind": "discord",
                "binding_id": "watched-discord",
            }
        },
    )

    assert decision.suppress_publish is True
    assert decision.requested_delivery == "suppressed_duplicate"
    assert decision.attempts == ()


def test_build_pma_dispatch_decision_rejects_unknown_explicit_target_and_falls_back(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo-a"

    decision = build_pma_dispatch_decision(
        message="Fallback",
        requested_delivery="auto",
        source_kind="managed_thread_completed",
        repo_id="repo-a",
        workspace_root=workspace,
        managed_thread_id="watched-thread",
        delivery_target={
            "surface_kind": "discord",
            "surface_key": "missing-discord",
        },
        context_payload={
            "wake_up": {
                "metadata": {
                    "pma_origin": {
                        "thread_id": "origin-thread",
                    }
                }
            }
        },
        binding_metadata_by_thread={
            "origin-thread": {
                "binding_kind": "telegram",
                "binding_id": "origin-telegram",
            }
        },
        preferred_bound_surface_kinds=("telegram", "discord"),
    )

    assert [attempt.route for attempt in decision.attempts] == [
        "primary_pma",
        "primary_pma",
        "bound",
        "bound",
    ]
    assert [attempt.surface_kind for attempt in decision.attempts] == [
        "discord",
        "telegram",
        "telegram",
        "discord",
    ]


def test_build_pma_dispatch_decision_does_not_suppress_non_terminal_source() -> None:
    decision = build_pma_dispatch_decision(
        message="Already handled. No action needed.",
        requested_delivery="auto",
        source_kind="automation",
        repo_id="repo-a",
        workspace_root=None,
        managed_thread_id="watched-thread",
        delivery_target={
            "surface_kind": "discord",
            "surface_key": "watched-discord",
        },
        context_payload=None,
        binding_metadata_by_thread={
            "watched-thread": {
                "binding_kind": "discord",
                "binding_id": "watched-discord",
            }
        },
    )

    assert decision.suppress_publish is False
    assert any(a.route == "explicit" for a in decision.attempts)


def test_build_pma_dispatch_decision_does_not_suppress_normal_completion_message() -> (
    None
):
    decision = build_pma_dispatch_decision(
        message="Changes pushed successfully.",
        requested_delivery="auto",
        source_kind="managed_thread_completed",
        repo_id="repo-a",
        workspace_root=None,
        managed_thread_id="watched-thread",
        delivery_target={
            "surface_kind": "discord",
            "surface_key": "watched-discord",
        },
        context_payload=None,
        binding_metadata_by_thread={
            "watched-thread": {
                "binding_kind": "discord",
                "binding_id": "watched-discord",
            }
        },
    )

    assert decision.suppress_publish is False
    assert any(a.route == "explicit" for a in decision.attempts)


def test_build_pma_dispatch_decision_does_not_suppress_when_binding_mismatches(
    tmp_path: Path,
) -> None:
    decision = build_pma_dispatch_decision(
        message="Already handled. No action needed.",
        requested_delivery="auto",
        source_kind="managed_thread_completed",
        repo_id="repo-a",
        workspace_root=tmp_path / "repo-a",
        managed_thread_id="watched-thread",
        delivery_target={
            "surface_kind": "discord",
            "surface_key": "other-discord",
        },
        context_payload=None,
        binding_metadata_by_thread={
            "watched-thread": {
                "binding_kind": "discord",
                "binding_id": "watched-discord",
            }
        },
        preferred_bound_surface_kinds=("discord", "telegram"),
    )

    assert decision.suppress_publish is False
    assert any(a.route == "primary_pma" for a in decision.attempts)


def test_build_pma_dispatch_decision_does_not_suppress_without_managed_thread() -> None:
    decision = build_pma_dispatch_decision(
        message="Already handled. No action needed.",
        requested_delivery="auto",
        source_kind="managed_thread_completed",
        repo_id="repo-a",
        workspace_root=None,
        managed_thread_id=None,
        delivery_target={
            "surface_kind": "discord",
            "surface_key": "some-discord",
        },
        context_payload=None,
        binding_metadata_by_thread={},
    )

    assert decision.suppress_publish is False


def test_build_pma_dispatch_decision_skips_explicit_without_binding_thread_ids(
    tmp_path: Path,
) -> None:
    """No managed thread or origin thread ids: do not persist an explicit attempt."""
    decision = build_pma_dispatch_decision(
        message="Hello",
        requested_delivery="auto",
        source_kind="automation",
        repo_id="repo-a",
        workspace_root=tmp_path / "repo-a",
        managed_thread_id=None,
        delivery_target={
            "surface_kind": "discord",
            "surface_key": "orphan-discord",
        },
        context_payload=None,
        binding_metadata_by_thread={
            "some-thread": {
                "binding_kind": "discord",
                "binding_id": "orphan-discord",
            }
        },
        preferred_bound_surface_kinds=("discord", "telegram"),
    )

    assert not any(a.route == "explicit" for a in decision.attempts)
    assert any(a.route == "primary_pma" for a in decision.attempts)


def test_build_pma_dispatch_decision_uses_wake_up_lane_delivery_target_without_binding(
    tmp_path: Path,
) -> None:
    decision = build_pma_dispatch_decision(
        message="Lane target",
        requested_delivery="auto",
        source_kind="managed_thread_completed",
        repo_id="repo-a",
        workspace_root=tmp_path / "repo-a",
        managed_thread_id="watched-thread",
        delivery_target=None,
        context_payload={
            "wake_up": {
                "lane_id": "discord:1497177978256232530",
            }
        },
        binding_metadata_by_thread={},
        preferred_bound_surface_kinds=("discord", "telegram"),
    )

    assert [attempt.route for attempt in decision.attempts] == [
        "explicit",
        "primary_pma",
        "primary_pma",
        "bound",
        "bound",
    ]
    assert decision.attempts[0].surface_kind == "discord"
    assert decision.attempts[0].surface_key == "1497177978256232530"


def test_build_pma_dispatch_decision_resolves_discord_lane_surface_key(
    tmp_path: Path,
) -> None:
    decision = build_pma_dispatch_decision(
        message="Lane-targeted follow-up",
        requested_delivery="auto",
        source_kind="automation",
        repo_id="repo-a",
        workspace_root=tmp_path / "repo-a",
        managed_thread_id="watched-thread",
        delivery_target=None,
        context_payload=None,
        binding_metadata_by_thread={},
        preferred_bound_surface_kinds=("discord", "telegram"),
        lane_id="discord:12345",
    )

    attempts_by_surface = {
        (attempt.route, attempt.surface_kind): attempt.surface_key
        for attempt in decision.attempts
    }

    assert attempts_by_surface[("explicit", "discord")] == "12345"
    assert attempts_by_surface[("primary_pma", "discord")] == "12345"
    assert attempts_by_surface[("primary_pma", "telegram")] is None
    assert attempts_by_surface[("bound", "discord")] == "12345"
    assert attempts_by_surface[("bound", "telegram")] is None


def test_build_pma_dispatch_decision_resolves_telegram_lane_surface_key(
    tmp_path: Path,
) -> None:
    decision = build_pma_dispatch_decision(
        message="Lane-targeted follow-up",
        requested_delivery="auto",
        source_kind="automation",
        repo_id="repo-a",
        workspace_root=tmp_path / "repo-a",
        managed_thread_id="watched-thread",
        delivery_target=None,
        context_payload=None,
        binding_metadata_by_thread={},
        preferred_bound_surface_kinds=("discord", "telegram"),
        lane_id="telegram:chat-id",
    )

    attempts_by_surface = {
        (attempt.route, attempt.surface_kind): attempt.surface_key
        for attempt in decision.attempts
    }

    assert attempts_by_surface[("explicit", "telegram")] == "chat-id"
    assert attempts_by_surface[("primary_pma", "discord")] is None
    assert attempts_by_surface[("primary_pma", "telegram")] == "chat-id"
    assert attempts_by_surface[("bound", "discord")] is None
    assert attempts_by_surface[("bound", "telegram")] == "chat-id"


def test_build_pma_dispatch_decision_keeps_surface_keys_empty_without_lane_id(
    tmp_path: Path,
) -> None:
    decision = build_pma_dispatch_decision(
        message="Lane-targeted follow-up",
        requested_delivery="auto",
        source_kind="automation",
        repo_id="repo-a",
        workspace_root=tmp_path / "repo-a",
        managed_thread_id="watched-thread",
        delivery_target=None,
        context_payload=None,
        binding_metadata_by_thread={},
        preferred_bound_surface_kinds=("discord", "telegram"),
    )

    assert [attempt.surface_key for attempt in decision.attempts] == [
        None,
        None,
        None,
        None,
    ]
