from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.pma_support.subscription_dispatch import (
    ChatBindingSpec,
    SubscriptionDispatchHarness,
    topic_key,
)


@dataclass(frozen=True)
class ManagedThreadTerminalCase:
    name: str
    rebind_origin_to: ChatBindingSpec | None
    rebind_origin_after_wakeup_to: ChatBindingSpec | None
    expected_route: str
    expected_discord_targets: tuple[str, ...]
    expected_telegram_targets: tuple[str, ...]


@pytest.mark.anyio
@pytest.mark.xfail(
    reason="dispatch_decision not yet written into wakeup metadata by publish flow"
)
@pytest.mark.parametrize(
    "case",
    [
        ManagedThreadTerminalCase(
            name="origin-thread-discord-target-is-used-for-terminal-followup",
            rebind_origin_to=None,
            rebind_origin_after_wakeup_to=None,
            expected_route="explicit",
            expected_discord_targets=("hermes-origin-discord",),
            expected_telegram_targets=(),
        ),
        ManagedThreadTerminalCase(
            name="origin-thread-rebind-forces-fallback-to-target-repo-pma-chat",
            rebind_origin_to=ChatBindingSpec(
                surface_kind="telegram",
                surface_key=topic_key(9101, 9202),
            ),
            rebind_origin_after_wakeup_to=None,
            expected_route="primary_pma",
            expected_discord_targets=(),
            expected_telegram_targets=(topic_key(7001, 8002),),
        ),
        ManagedThreadTerminalCase(
            name="post-wakeup-rebind-does-not-change-persisted-explicit-route",
            rebind_origin_to=None,
            rebind_origin_after_wakeup_to=ChatBindingSpec(
                surface_kind="telegram",
                surface_key=topic_key(9301, 9402),
            ),
            expected_route="explicit",
            expected_discord_targets=("hermes-origin-discord",),
            expected_telegram_targets=(),
        ),
    ],
    ids=lambda case: case.name,
)
async def test_managed_thread_terminal_dispatch_state_machine(
    tmp_path: Path,
    case: ManagedThreadTerminalCase,
) -> None:
    harness = SubscriptionDispatchHarness(tmp_path)
    harness.add_repo("codex-autorunner")
    harness.add_repo("hermes-agent")

    await harness.seed_binding(
        repo_id="hermes-agent",
        binding=ChatBindingSpec(
            surface_kind="discord",
            surface_key="hermes-origin-discord",
        ),
    )
    await harness.seed_binding(
        repo_id="codex-autorunner",
        binding=ChatBindingSpec(
            surface_kind="telegram",
            surface_key=topic_key(7001, 8002),
            pma_enabled=True,
        ),
    )
    if (
        case.rebind_origin_to is not None
        and case.rebind_origin_to.surface_kind == "telegram"
    ):
        await harness.seed_binding(
            repo_id="hermes-agent",
            binding=case.rebind_origin_to,
        )

    origin_thread_id = harness.create_thread(
        repo_id="hermes-agent",
        binding=ChatBindingSpec(
            surface_kind="discord",
            surface_key="hermes-origin-discord",
        ),
        agent="hermes",
    )
    watched_thread_id = harness.create_thread(repo_id="codex-autorunner")

    snapshot = await harness.run_managed_thread_terminal_case(
        watched_repo_id="codex-autorunner",
        watched_thread_id=watched_thread_id,
        origin_thread_id=origin_thread_id,
        rebind_origin_to=case.rebind_origin_to,
        rebind_origin_after_wakeup_to=case.rebind_origin_after_wakeup_to,
    )

    assert snapshot.subscription["thread_id"] == watched_thread_id
    assert snapshot.subscription["lane_id"] == "discord"
    assert snapshot.subscription["metadata"]["delivery_target"] == {
        "surface_kind": "discord",
        "surface_key": "hermes-origin-discord",
    }
    assert snapshot.subscription["metadata"]["pma_origin"] == {
        "thread_id": origin_thread_id,
    }
    assert snapshot.transition == {
        "status": "ok",
        "matched": 1,
        "created": 1,
        "repo_id": "codex-autorunner",
        "run_id": None,
        "thread_id": watched_thread_id,
        "from_state": "running",
        "to_state": "completed",
        "reason": "transition",
        "timestamp": snapshot.transition["timestamp"],
    }
    assert snapshot.wake_up["metadata"]["delivery_target"] == {
        "surface_kind": "discord",
        "surface_key": "hermes-origin-discord",
    }
    first_attempt = snapshot.wake_up["metadata"]["dispatch_decision"]["attempts"][0]
    if case.expected_route == "explicit":
        assert first_attempt == {
            "route": "explicit",
            "delivery_mode": "bound",
            "surface_kind": "discord",
            "surface_key": "hermes-origin-discord",
            "repo_id": "codex-autorunner",
            "workspace_root": None,
        }
    else:
        assert first_attempt["route"] == "primary_pma"
        assert first_attempt["surface_kind"] in {"discord", "telegram"}
    assert snapshot.wake_up["metadata"]["pma_origin"] == {
        "thread_id": origin_thread_id,
    }
    assert snapshot.publish_result["delivery_outcome"]["route"] == case.expected_route
    assert snapshot.discord_targets == case.expected_discord_targets
    assert snapshot.telegram_targets == case.expected_telegram_targets
