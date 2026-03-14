from __future__ import annotations

import os
from pathlib import Path

import pytest

from codex_autorunner.agents.zeroclaw.harness import ZeroClawHarness
from codex_autorunner.agents.zeroclaw.supervisor import (
    build_zeroclaw_supervisor_from_config,
    zeroclaw_binary_available,
)
from codex_autorunner.core.config import load_repo_config

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def skip_unless_opted_in() -> None:
    if os.environ.get("ZEROCLAW_DOGFOOD") != "1":
        pytest.skip("Set ZEROCLAW_DOGFOOD=1 to run live ZeroClaw host integration.")


@pytest.mark.asyncio
async def test_zeroclaw_host_single_turn_round_trip() -> None:
    config = load_repo_config(Path("."))
    if not zeroclaw_binary_available(config):
        pytest.skip("ZeroClaw binary not available through CAR config.")

    model = os.environ.get("ZEROCLAW_TEST_MODEL", "zai/glm-5")
    prompt = os.environ.get(
        "ZEROCLAW_TEST_PROMPT",
        "Reply with the exact text ZC-HOST-OK and nothing else.",
    )
    expected = os.environ.get("ZEROCLAW_EXPECTED_SUBSTRING", "ZC-HOST-OK")

    supervisor = build_zeroclaw_supervisor_from_config(config)
    assert supervisor is not None
    harness = ZeroClawHarness(supervisor)
    workspace_root = Path(".").resolve()

    try:
        conversation = await harness.new_conversation(
            workspace_root,
            title="ZeroClaw host dogfood",
        )
        turn = await harness.start_turn(
            workspace_root,
            conversation.id,
            prompt=prompt,
            model=model,
            reasoning=None,
            approval_mode=None,
            sandbox_policy=None,
        )
        result = await harness.wait_for_turn(
            workspace_root,
            conversation.id,
            turn.turn_id,
            timeout=90,
        )
    finally:
        await supervisor.close_all()

    assert result.status == "completed"
    assert expected in result.assistant_text
    assert result.errors == []
