from typing import Any

import pytest

from codex_autorunner.integrations.app_server.client import CodexAppServerResponseError
from codex_autorunner.integrations.telegram.handlers.commands.workspace import (
    _model_list_with_agent_compat,
)


class _StubClient:
    def __init__(
        self,
        *,
        response: Any,
        fail_agent_request: bool = False,
        fail_code: int = -32602,
    ) -> None:
        self._response = response
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
        return self._response


@pytest.mark.asyncio
async def test_model_list_with_agent_compat_uses_agent_filter() -> None:
    client = _StubClient(response={"data": [{"id": "gpt-5.3-codex-spark"}]})

    result = await _model_list_with_agent_compat(
        client,
        params={"agent": "codex", "limit": 25, "cursor": None},
    )

    assert result == {"data": [{"id": "gpt-5.3-codex-spark"}]}
    assert client.calls == [{"agent": "codex", "limit": 25, "cursor": None}]


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
        {"agent": "codex", "limit": 25, "cursor": None},
        {"limit": 25, "cursor": None},
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

    assert client.calls == [{"agent": "codex", "limit": 25, "cursor": None}]


@pytest.mark.asyncio
async def test_model_list_with_agent_compat_without_agent_keeps_params() -> None:
    client = _StubClient(response={"data": [{"id": "gpt-5.3-codex-spark"}]})

    result = await _model_list_with_agent_compat(
        client,
        params={"limit": 10, "cursor": "next-cursor"},
    )

    assert result == {"data": [{"id": "gpt-5.3-codex-spark"}]}
    assert client.calls == [{"limit": 10, "cursor": "next-cursor"}]
