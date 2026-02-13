from typing import Any, Dict, Optional

import pytest

from codex_autorunner.integrations.app_server.client import CodexAppServerResponseError
from codex_autorunner.integrations.telegram.handlers.commands.workspace import (
    _model_list_all_with_agent_compat,
    _model_list_with_agent_compat,
)


class _StubClient:
    def __init__(
        self,
        *,
        response: Any,
        responses_by_cursor: Optional[Dict[Optional[str], Any]] = None,
        fail_agent_request: bool = False,
        fail_code: int = -32602,
    ) -> None:
        self._response = response
        self._responses_by_cursor = responses_by_cursor
        self._fail_agent_request = fail_agent_request
        self._fail_code = fail_code
        self.calls: list[dict[str, Any]] = []

    async def model_list(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if kwargs.get("agent") == "codex" and self._fail_agent_request:
            raise CodexAppServerResponseError(
                method="model/list",
                code=self._fail_code,
                message="invalid params",
            )
        if self._responses_by_cursor is not None:
            cursor = kwargs.get("cursor")
            if cursor in self._responses_by_cursor:
                return self._responses_by_cursor[cursor]
        return self._response


@pytest.mark.asyncio
async def test_model_list_with_agent_compat_uses_agent_filter() -> None:
    client = _StubClient(response={"data": [{"id": "gpt-5.3-codex-spark"}]})

    result = await _model_list_with_agent_compat(
        client,
        params={"agent": "codex", "limit": 25, "cursor": None},
    )

    assert result == {"data": [{"id": "gpt-5.3-codex-spark"}]}
    assert client.calls == [{"agent": "codex", "limit": 25}]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_code", [-32600, -32602])
async def test_model_list_with_agent_compat_falls_back_for_invalid_params(
    fail_code: int,
) -> None:
    client = _StubClient(
        response={"data": [{"id": "gpt-5.3-codex-spark"}]},
        fail_agent_request=True,
        fail_code=fail_code,
    )

    result = await _model_list_with_agent_compat(
        client,
        params={"agent": "codex", "limit": 25, "cursor": None},
    )

    assert result == {"data": [{"id": "gpt-5.3-codex-spark"}]}
    assert client.calls == [
        {"agent": "codex", "limit": 25},
        {"limit": 25},
    ]


@pytest.mark.asyncio
async def test_model_list_with_agent_compat_raises_non_compat_errors() -> None:
    client = _StubClient(
        response={"data": []},
        fail_agent_request=True,
        fail_code=-32001,
    )

    with pytest.raises(CodexAppServerResponseError):
        await _model_list_with_agent_compat(
            client,
            params={"agent": "codex", "limit": 25, "cursor": None},
        )

    assert client.calls == [{"agent": "codex", "limit": 25}]


@pytest.mark.asyncio
async def test_model_list_with_agent_compat_without_agent_keeps_params() -> None:
    client = _StubClient(response={"data": [{"id": "gpt-5.3-codex-spark"}]})

    result = await _model_list_with_agent_compat(
        client,
        params={"limit": 10, "cursor": "next-cursor"},
    )

    assert result == {"data": [{"id": "gpt-5.3-codex-spark"}]}
    assert client.calls == [{"limit": 10, "cursor": "next-cursor"}]


@pytest.mark.asyncio
async def test_model_list_with_agent_compat_drops_none_params() -> None:
    client = _StubClient(response={"data": [{"id": "gpt-5.3-codex-spark"}]})

    result = await _model_list_with_agent_compat(
        client,
        params={"agent": "codex", "limit": 25, "cursor": None, "foo": None},
    )

    assert result == {"data": [{"id": "gpt-5.3-codex-spark"}]}
    assert client.calls == [{"agent": "codex", "limit": 25}]


@pytest.mark.asyncio
async def test_model_list_all_with_agent_compat_follows_next_cursor() -> None:
    client = _StubClient(
        response={"data": []},
        responses_by_cursor={
            None: {
                "data": [
                    {"id": "gpt-5.3-codex"},
                    {"id": "gpt-5.2-codex"},
                ],
                "nextCursor": "cursor-2",
            },
            "cursor-2": {
                "data": [
                    {"id": "gpt-5.3-codex-spark"},
                    {"id": "gpt-5.1-codex-max"},
                ],
                "nextCursor": None,
            },
        },
    )

    result = await _model_list_all_with_agent_compat(
        client,
        params={"agent": "codex", "limit": 25, "cursor": None},
    )

    assert [entry["id"] for entry in result] == [
        "gpt-5.3-codex",
        "gpt-5.2-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.1-codex-max",
    ]
    assert client.calls == [
        {"agent": "codex", "limit": 25},
        {"agent": "codex", "limit": 25, "cursor": "cursor-2"},
    ]


@pytest.mark.asyncio
async def test_model_list_all_with_agent_compat_deduplicates_models() -> None:
    client = _StubClient(
        response={"data": []},
        responses_by_cursor={
            None: {
                "data": [
                    {"id": "gpt-5.3-codex"},
                    {"id": "gpt-5.3-codex-spark"},
                ],
                "nextCursor": "cursor-2",
            },
            "cursor-2": {
                "data": [
                    {"id": "gpt-5.3-codex-spark"},
                    {"id": "gpt-5.2-codex"},
                ],
                "nextCursor": None,
            },
        },
    )

    result = await _model_list_all_with_agent_compat(
        client,
        params={"agent": "codex", "limit": 25},
    )

    assert [entry["id"] for entry in result] == [
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.2-codex",
    ]
