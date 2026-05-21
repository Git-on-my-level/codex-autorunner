import json

import pytest

from codex_autorunner.core.sse import parse_sse_lines


@pytest.mark.asyncio
async def test_parse_sse_lines_parses_named_and_default_events() -> None:
    async def _lines():
        yield "event: message"
        yield 'data: {"type": "test", "value": 42}'
        yield ""
        yield "event: custom"
        yield "data: hello"
        yield ""

    events = [event async for event in parse_sse_lines(_lines())]

    assert len(events) == 2
    assert events[0].event == "message"
    assert json.loads(events[0].data) == {"type": "test", "value": 42}
    assert events[1].event == "custom"
    assert events[1].data == "hello"
