from __future__ import annotations

import uuid

import pytest

from codex_autorunner.core.orchestration.thread_titles import (
    ManagedThreadTitleInputs,
    choose_owned_thread_title,
    resolve_managed_thread_display_title,
)


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (
            ManagedThreadTitleInputs(
                stored_title="Release investigation",
                provider_title="Provider title",
                user_visible_title_seed="Visible request",
            ),
            "Release investigation",
        ),
        (
            ManagedThreadTitleInputs(
                stored_title="New chat",
                provider_title="Native Codex thread",
                user_visible_title_seed="Visible request",
            ),
            "Native Codex thread",
        ),
        (
            ManagedThreadTitleInputs(
                stored_title="New coding agent chat",
                user_visible_title_seed="Test preview services",
            ),
            "Test preview services",
        ),
        (
            ManagedThreadTitleInputs(
                stored_title="discord:1488827014600331415",
                user_visible_title_seed="Investigate notification routing",
                chat_display_name="CAR Workspace / #hermes",
            ),
            "Investigate notification routing",
        ),
        (
            ManagedThreadTitleInputs(
                stored_title="New chat",
                chat_display_name="Deploys / Thread 12",
                fallback_id="thread-1",
            ),
            "Deploys / Thread 12",
        ),
        (
            ManagedThreadTitleInputs(
                stored_title=(
                    "<CAR_TICKET_FLOW_PROMPT><CAR_CURRENT_TICKET_FILE />"
                    "</CAR_TICKET_FLOW_PROMPT>"
                ),
                ticket_id="TICKET-002",
                fallback_id="thread-ticket-flow",
            ),
            "Ticket flow · TICKET-002",
        ),
        (
            ManagedThreadTitleInputs(
                stored_title=str(uuid.UUID("12345678-1234-5678-1234-567812345678")),
                provider_title="",
                user_visible_title_seed="Fix Discord title regression",
                fallback_id="thread-uuid",
            ),
            "Fix Discord title regression",
        ),
        (
            ManagedThreadTitleInputs(
                stored_title="Thread thread-no-message",
                fallback_id="thread-no-message",
            ),
            "thread-no-message",
        ),
    ],
)
def test_resolve_managed_thread_display_title_policy_matrix(
    inputs: ManagedThreadTitleInputs,
    expected: str,
) -> None:
    assert resolve_managed_thread_display_title(inputs) == expected


def test_choose_owned_thread_title_uses_central_policy() -> None:
    assert (
        choose_owned_thread_title(
            "Thread thread-1",
            provider_title=None,
            message_preview="Compare chat title sources",
            fallback="thread-1",
        )
        == "Compare chat title sources"
    )
