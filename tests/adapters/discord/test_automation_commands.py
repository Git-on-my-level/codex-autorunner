from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.adapters.discord.automation_commands import (
    handle_automation_run,
    handle_automation_status,
)
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.automation import AutomationStore
from codex_autorunner.core.automation.models import EXECUTOR_AGENT_TASK_TURN
from codex_autorunner.core.automation.product import (
    AutomationPresetRequest,
    create_preset_automation,
)


class _DiscordAutomationService:
    def __init__(self, root: Any) -> None:
        self._config = SimpleNamespace(root=root)
        self._hub_supervisor = None
        self.messages: list[str] = []

    async def send_or_respond_ephemeral(
        self, interaction_id: str, interaction_token: str, text: str
    ) -> None:
        _ = interaction_id, interaction_token
        self.messages.append(text)


@pytest.mark.asyncio
async def test_discord_automation_status_shows_execution_mode(tmp_path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    automation = create_preset_automation(
        AutomationStore(hub_root),
        AutomationPresetRequest(
            preset="security_scan_pr",
            repo_id="repo-1",
            agent="codex",
            model="gpt-5.4",
        ),
    )
    service = _DiscordAutomationService(hub_root)

    await handle_automation_status(
        service,
        "interaction-1",
        "token-1",
        options={"id": automation["id"]},
    )

    assert "Execution mode: agent_task_turn" in service.messages[-1]
    assert "Runtime: direct codex / gpt-5.4" in service.messages[-1]


@pytest.mark.asyncio
async def test_discord_automation_run_uses_selected_execution_mode(tmp_path) -> None:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    automation = create_preset_automation(
        AutomationStore(hub_root),
        AutomationPresetRequest(
            preset="security_scan_pr",
            execution_mode=EXECUTOR_AGENT_TASK_TURN,
            repo_id="repo-1",
            agent="codex",
        ),
    )
    service = _DiscordAutomationService(hub_root)

    await handle_automation_run(
        service,
        "interaction-1",
        "token-1",
        options={"id": automation["id"]},
    )

    assert "Execution mode: agent_task_turn" in service.messages[-1]
    assert "Jobs created: 1" in service.messages[-1]
    jobs = AutomationStore(hub_root).list_jobs(rule_id=automation["id"])
    assert jobs[0].executor["kind"] == EXECUTOR_AGENT_TASK_TURN
