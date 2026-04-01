import json
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import httpx
import pytest

from codex_autorunner.agents.opencode.client import (
    OpenCodeApiProfile,
    OpenCodeClient,
    _normalize_sse_event,
    _normalize_template_path,
)
from codex_autorunner.core.sse import SSEEvent


def test_normalize_sse_event_unwraps_payload() -> None:
    event = SSEEvent(
        event="message",
        data=(
            '{"directory":"/repo","payload":{"type":"message.part.updated","properties":'
            '{"sessionID":"s1"}}}'
        ),
    )
    normalized = _normalize_sse_event(event)
    assert normalized.event == "message.part.updated"
    assert json.loads(normalized.data) == {
        "type": "message.part.updated",
        "properties": {"sessionID": "s1"},
    }


def test_normalize_sse_event_uses_payload_type() -> None:
    event = SSEEvent(
        event="message",
        data='{"type":"session.idle","sessionID":"s1"}',
    )
    normalized = _normalize_sse_event(event)
    assert normalized.event == "session.idle"
    assert json.loads(normalized.data) == {"type": "session.idle", "sessionID": "s1"}


def test_normalize_sse_event_keeps_non_json() -> None:
    event = SSEEvent(event="message", data="ping")
    normalized = _normalize_sse_event(event)
    assert normalized.event == "message"
    assert normalized.data == "ping"


def test_normalize_sse_event_preserves_wrapper_metadata() -> None:
    event = SSEEvent(
        event="message",
        data='{"type":"session.status","sessionID":"s42","payload":{"state":"running"}}',
    )
    normalized = _normalize_sse_event(event)
    payload = json.loads(normalized.data)
    assert payload["sessionID"] == "s42"
    assert payload.get("state") == "running"
    assert normalized.event == "session.status"


def test_normalize_template_path_matches_placeholder_names() -> None:
    normalized = _normalize_template_path("/session/{sessionID}/prompt_async")
    assert normalized == "/session/{}/prompt_async"
    # Different placeholder names should normalize identically
    assert normalized == _normalize_template_path("/session/{session_id}/prompt_async")


class _FakeStreamResponse:
    def __init__(self, status_code: int, lines: Optional[List[str]] = None):
        self.status_code = status_code
        self._lines = lines or []
        self.headers: Dict[str, str] = {"content-type": "text/event-stream"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=MagicMock(),
                response=self,  # type: ignore
            )

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeAsyncClient:
    def __init__(self, responses: Dict[str, _FakeStreamResponse]):
        self._responses = responses
        self.stream_calls: List[Tuple[str, str, Dict[str, Any]]] = []

    def stream(self, method: str, path: str, **kwargs):
        self.stream_calls.append((method, path, kwargs))
        response = self._responses.get(path)
        if response is None:
            response = _FakeStreamResponse(404)
        return _FakeStreamContextManager(response)


class _FakeStreamContextManager:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        pass


@pytest.mark.anyio
async def test_stream_events_fallback_from_404() -> None:
    """Characterization: stream_events should fall back to next path on 404."""
    event_data = 'data: {"type":"test","sessionID":"s1"}\n\n'
    responses = {
        "/global/event": _FakeStreamResponse(404),
        "/event": _FakeStreamResponse(200, lines=[event_data]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore
    client._api_profile = OpenCodeApiProfile(supports_global_endpoints=True)

    events = []
    async for event in client.stream_events(directory=None):
        events.append(event)

    assert len(events) == 1
    assert events[0].event == "test"
    assert fake_client.stream_calls[0] == (
        "GET",
        "/global/event",
        {"params": {}, "timeout": None},
    )
    assert fake_client.stream_calls[1] == (
        "GET",
        "/event",
        {"params": {}, "timeout": None},
    )


@pytest.mark.anyio
async def test_stream_events_fallback_from_405() -> None:
    """Characterization: stream_events should fall back to next path on 405."""
    event_data = 'data: {"type":"test","sessionID":"s1"}\n\n'
    responses = {
        "/global/event": _FakeStreamResponse(405),
        "/event": _FakeStreamResponse(200, lines=[event_data]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore
    client._api_profile = OpenCodeApiProfile(supports_global_endpoints=True)

    events = []
    async for event in client.stream_events(directory=None):
        events.append(event)

    assert len(events) == 1
    assert len(fake_client.stream_calls) == 2


@pytest.mark.anyio
async def test_stream_events_raises_on_500() -> None:
    """Characterization: stream_events should raise on 500, not fall back."""
    responses = {
        "/global/event": _FakeStreamResponse(500),
        "/event": _FakeStreamResponse(200, lines=["data: ok\n\n"]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore
    client._api_profile = OpenCodeApiProfile(supports_global_endpoints=True)

    with pytest.raises(httpx.HTTPStatusError):
        async for _ in client.stream_events(directory=None):
            pass

    assert len(fake_client.stream_calls) == 1


@pytest.mark.anyio
async def test_stream_events_with_custom_paths() -> None:
    """Characterization: stream_events should use custom paths when provided."""
    event_data = 'data: {"type":"custom"}\n\n'
    responses = {
        "/session/s1/event": _FakeStreamResponse(200, lines=[event_data]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore

    events = []
    async for event in client.stream_events(paths=["/session/s1/event"]):
        events.append(event)

    assert len(events) == 1
    assert events[0].event == "custom"
    assert fake_client.stream_calls[0][1] == "/session/s1/event"


@pytest.mark.anyio
async def test_stream_events_path_order_with_global_support() -> None:
    """Characterization: path order depends on directory and global endpoint support."""
    responses = {
        "/global/event": _FakeStreamResponse(200, lines=["data: ok\n\n"]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore
    client._api_profile = OpenCodeApiProfile(supports_global_endpoints=True)

    async for _ in client.stream_events(directory="/workspace"):
        pass

    assert fake_client.stream_calls[0][1] == "/event"
    assert fake_client.stream_calls[1][1] == "/global/event"


@pytest.mark.anyio
async def test_stream_events_path_order_without_directory() -> None:
    """Characterization: without directory, global endpoint is tried first."""
    responses = {
        "/global/event": _FakeStreamResponse(200, lines=["data: ok\n\n"]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore
    client._api_profile = OpenCodeApiProfile(supports_global_endpoints=True)

    async for _ in client.stream_events(directory=None):
        pass

    assert fake_client.stream_calls[0][1] == "/global/event"
    assert len(fake_client.stream_calls) == 1


@pytest.mark.anyio
async def test_stream_events_no_fallback_without_global_support() -> None:
    """Characterization: without global endpoint support, only /event is tried."""
    responses = {
        "/event": _FakeStreamResponse(200, lines=["data: ok\n\n"]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore
    client._api_profile = OpenCodeApiProfile(supports_global_endpoints=False)

    async for _ in client.stream_events(directory=None):
        pass

    assert len(fake_client.stream_calls) == 1
    assert fake_client.stream_calls[0][1] == "/event"


@pytest.mark.anyio
async def test_stream_events_with_session_id_prepends_session_path() -> None:
    """Characterization: session_id parameter adds session-scoped path first."""
    event_data = 'data: {"type":"test","sessionID":"s1"}\n\n'
    responses = {
        "/session/s1/event": _FakeStreamResponse(200, lines=[event_data]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore
    client._api_profile = OpenCodeApiProfile(supports_global_endpoints=True)

    events = []
    async for event in client.stream_events(directory=None, session_id="s1"):
        events.append(event)

    assert len(events) == 1
    assert events[0].event == "test"
    assert fake_client.stream_calls[0][1] == "/session/s1/event"


@pytest.mark.anyio
async def test_stream_events_with_session_id_falls_back() -> None:
    """Characterization: session-scoped path falls back to global on 404."""
    event_data = 'data: {"type":"test","sessionID":"s1"}\n\n'
    responses = {
        "/session/s1/event": _FakeStreamResponse(404),
        "/global/event": _FakeStreamResponse(200, lines=[event_data]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore
    client._api_profile = OpenCodeApiProfile(supports_global_endpoints=True)

    events = []
    async for event in client.stream_events(directory=None, session_id="s1"):
        events.append(event)

    assert len(events) == 1
    assert fake_client.stream_calls[0][1] == "/session/s1/event"
    assert fake_client.stream_calls[1][1] == "/global/event"


@pytest.mark.anyio
async def test_stream_events_custom_paths_override_session_id() -> None:
    """Characterization: custom paths override session_id path construction."""
    event_data = 'data: {"type":"custom"}\n\n'
    responses = {
        "/custom/event": _FakeStreamResponse(200, lines=[event_data]),
    }
    fake_client = _FakeAsyncClient(responses)

    client = OpenCodeClient(base_url="http://test")
    client._client = fake_client  # type: ignore

    events = []
    async for event in client.stream_events(paths=["/custom/event"], session_id="s1"):
        events.append(event)

    assert len(events) == 1
    assert len(fake_client.stream_calls) == 1
    assert fake_client.stream_calls[0][1] == "/custom/event"
