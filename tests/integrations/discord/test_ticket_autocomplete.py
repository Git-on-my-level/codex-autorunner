from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codex_autorunner.integrations.discord.car_autocomplete import (
    build_ticket_autocomplete_choices,
)


async def _acoro(value):
    return value


def _make_async_service(
    *,
    workspace_root: Path | None = Path("/fake/workspace"),
    ticket_choices: list[tuple[str, str, str]] | None = None,
    list_choices_fn=None,
):
    service = SimpleNamespace()
    if workspace_root is not None:
        service._bound_workspace_root_for_channel = Mock(
            return_value=_acoro(workspace_root)
        )
    else:
        service._bound_workspace_root_for_channel = Mock(return_value=_acoro(None))
    service._pending_ticket_filters = {"chan-1": "all"}
    if list_choices_fn is not None:
        service._list_ticket_choices = list_choices_fn
    elif ticket_choices is not None:
        service._list_ticket_choices = Mock(return_value=ticket_choices)
    return service


@pytest.mark.anyio
async def test_returns_empty_when_no_workspace_bound() -> None:
    service = _make_async_service(workspace_root=None)
    result = await build_ticket_autocomplete_choices(
        service, channel_id="chan-1", query=""
    )
    assert result == []


@pytest.mark.anyio
async def test_returns_choices_on_normal_path() -> None:
    service = _make_async_service(
        ticket_choices=[
            (".codex-autorunner/tickets/TICKET-001.md", "TICKET-001.md", "open"),
        ]
    )
    result = await build_ticket_autocomplete_choices(
        service, channel_id="chan-1", query=""
    )
    assert len(result) == 1
    assert result[0]["value"] == ".codex-autorunner/tickets/TICKET-001.md"


@pytest.mark.anyio
async def test_returns_empty_on_exception() -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("filesystem on fire")

    service = _make_async_service(list_choices_fn=_boom)
    result = await build_ticket_autocomplete_choices(
        service, channel_id="chan-1", query=""
    )
    assert result == []


@pytest.mark.anyio
async def test_returns_empty_on_slow_scan() -> None:
    def _slow(*args, **kwargs):
        import time

        time.sleep(5)
        return [(".codex-autorunner/tickets/TICKET-001.md", "TICKET-001.md", "open")]

    service = _make_async_service(list_choices_fn=_slow)
    result = await build_ticket_autocomplete_choices(
        service, channel_id="chan-1", query=""
    )
    assert result == []
