import time
from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.constants import (
    REASONING_BUFFER_TTL_SECONDS,
    SELECTION_STATE_TTL_SECONDS,
)
from codex_autorunner.integrations.telegram.service import TelegramBotService
from codex_autorunner.integrations.telegram.types import SelectionState


@pytest.mark.anyio
async def test_cache_cleanup_eviction(tmp_path: Path) -> None:
    config = TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
        },
        root=tmp_path,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )
    service = TelegramBotService(config)
    try:
        service._reasoning_buffers["item-1"] = "buffer"
        service._cache_timestamps["reasoning_buffers"] = {
            "item-1": time.monotonic() - REASONING_BUFFER_TTL_SECONDS - 1.0
        }
        service._evict_expired_cache_entries(
            "reasoning_buffers", REASONING_BUFFER_TTL_SECONDS
        )
        assert "item-1" not in service._reasoning_buffers

        state = SelectionState(items=[("a", "A")], page=0)
        service._resume_options["topic-1"] = state
        service._cache_timestamps["resume_options"] = {
            "topic-1": time.monotonic() - SELECTION_STATE_TTL_SECONDS - 1.0
        }
        service._evict_expired_cache_entries(
            "resume_options", SELECTION_STATE_TTL_SECONDS
        )
        assert "topic-1" not in service._resume_options

        state = SelectionState(items=[("run-1", "Run 1")], page=0)
        service._flow_run_options["topic-2"] = state
        service._cache_timestamps["flow_run_options"] = {
            "topic-2": time.monotonic() - SELECTION_STATE_TTL_SECONDS - 1.0
        }
        service._evict_expired_cache_entries(
            "flow_run_options", SELECTION_STATE_TTL_SECONDS
        )
        assert "topic-2" not in service._flow_run_options
    finally:
        await service._bot.close()
