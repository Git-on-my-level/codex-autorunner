from __future__ import annotations

import pytest

from codex_autorunner.core.orchestration.thread_titles import (
    ManagedThreadTitleInputs,
    resolve_managed_thread_display_title,
)


@pytest.mark.parametrize(
    ("surface", "inputs", "expected"),
    [
        (
            "discord",
            ManagedThreadTitleInputs(
                stored_title="discord:1488827014600331415",
                user_visible_title_seed="Investigate Discord delivery",
                chat_display_name="CAR Workspace / #ops",
                fallback_id="thread-discord",
            ),
            "Investigate Discord delivery",
        ),
        (
            "telegram",
            ManagedThreadTitleInputs(
                stored_title="telegram:-1001:77",
                chat_display_name="Release room / Deploys",
                fallback_id="thread-telegram",
            ),
            "Release room / Deploys",
        ),
        (
            "web_pma",
            ManagedThreadTitleInputs(
                stored_title="New chat",
                provider_title="Native provider title",
                user_visible_title_seed="Lower priority visible seed",
                fallback_id="thread-web",
            ),
            "Native provider title",
        ),
    ],
)
def test_chat_adapter_managed_thread_title_policy_contract(
    surface: str,
    inputs: ManagedThreadTitleInputs,
    expected: str,
) -> None:
    assert surface
    assert resolve_managed_thread_display_title(inputs) == expected
