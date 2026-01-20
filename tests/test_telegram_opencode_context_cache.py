from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
)


class _FakeOpenCodeClient:
    async def providers(self, directory: Optional[str] = None) -> dict[str, object]:
        context = 1000
        if directory and directory.endswith("workspace_b"):
            context = 2000
        return {
            "providers": [
                {
                    "id": "provider",
                    "models": {
                        "model-a": {
                            "limit": {"context": context},
                        }
                    },
                }
            ]
        }


@pytest.mark.anyio
async def test_opencode_context_cache_scoped_by_workspace() -> None:
    handler = TelegramCommandHandlers()
    client = _FakeOpenCodeClient()
    model_payload = {"providerID": "provider", "modelID": "model-a"}
    workspace_a = Path("/tmp/workspace_a")
    workspace_b = Path("/tmp/workspace_b")

    context_a = await handler._resolve_opencode_model_context_window(
        client, workspace_a, model_payload
    )
    context_b = await handler._resolve_opencode_model_context_window(
        client, workspace_b, model_payload
    )

    assert context_a == 1000
    assert context_b == 2000
