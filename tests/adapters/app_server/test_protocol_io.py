import asyncio
import json
import logging
from typing import Any

import pytest

from codex_autorunner.adapters.app_server.errors import CodexAppServerResponseError
from codex_autorunner.adapters.app_server.protocol_io import AppServerProtocolIO


class _FakeStdin:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.lines.append(json.loads(data.decode("utf-8")))

    async def drain(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()


def _protocol(process: _FakeProcess) -> AppServerProtocolIO:
    return AppServerProtocolIO(
        process_getter=lambda: process,  # type: ignore[return-value]
        request_timeout=None,
        logger=logging.getLogger("test.app_server.protocol_io"),
        summarize_params=lambda _method, _params: {},
    )


@pytest.mark.anyio
async def test_protocol_io_matches_out_of_order_responses() -> None:
    process = _FakeProcess()
    protocol = _protocol(process)

    slow = asyncio.create_task(protocol.request("fixture/slow", {"value": "slow"}))
    fast = asyncio.create_task(protocol.request("fixture/fast", {"value": "fast"}))
    while len(process.stdin.lines) < 2:
        await asyncio.sleep(0)

    slow_id = process.stdin.lines[0]["id"]
    fast_id = process.stdin.lines[1]["id"]
    await protocol.handle_response({"id": fast_id, "result": {"value": "fast"}})
    await protocol.handle_response({"id": slow_id, "result": {"value": "slow"}})

    assert await fast == {"value": "fast"}
    assert await slow == {"value": "slow"}
    assert protocol.pending_request_count == 0


@pytest.mark.anyio
async def test_protocol_io_sets_response_error_on_pending_request() -> None:
    process = _FakeProcess()
    protocol = _protocol(process)

    request_task = asyncio.create_task(protocol.request("fixture/fail", None))
    while not process.stdin.lines:
        await asyncio.sleep(0)

    await protocol.handle_response(
        {
            "id": process.stdin.lines[0]["id"],
            "error": {"code": -32000, "message": "boom", "data": {"x": 1}},
        }
    )

    with pytest.raises(CodexAppServerResponseError) as exc_info:
        await request_task
    assert exc_info.value.code == -32000
    assert exc_info.value.method == "fixture/fail"
