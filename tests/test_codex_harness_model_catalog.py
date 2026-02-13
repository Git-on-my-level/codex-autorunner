from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.agents.codex.harness import CodexHarness
from codex_autorunner.integrations.app_server.client import CodexAppServerResponseError


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


class _StubSupervisor:
    def __init__(self, client: _StubClient) -> None:
        self._client = client

    async def get_client(self, _workspace_root: Path) -> _StubClient:
        return self._client


@pytest.mark.asyncio
async def test_model_catalog_uses_codex_agent_filter_and_normalizes_alias_name() -> (
    None
):
    client = _StubClient(
        response={
            "data": [
                {
                    "id": "gpt-5.3-codex-spark",
                    "displayName": "GPT-5.3-Codex-Spark",
                    "supportedReasoningEfforts": ["low", "medium", "high"],
                    "defaultReasoningEffort": "medium",
                },
                {
                    "id": "internal-preview-model",
                    "displayName": "Internal Preview (Fast)",
                },
            ]
        }
    )
    harness = CodexHarness(_StubSupervisor(client), events=object())  # type: ignore[arg-type]

    catalog = await harness.model_catalog(Path("."))

    assert client.calls == [{"agent": "codex"}]
    assert catalog.default_model == "gpt-5.3-codex-spark"
    assert [model.id for model in catalog.models] == [
        "gpt-5.3-codex-spark",
        "internal-preview-model",
    ]
    assert [model.display_name for model in catalog.models] == [
        "gpt-5.3-codex-spark",
        "Internal Preview (Fast)",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_code", [-32600, -32602])
async def test_model_catalog_falls_back_when_agent_filter_is_unsupported(
    fail_code: int,
) -> None:
    client = _StubClient(
        response={"data": [{"id": "gpt-5.3-codex-spark"}]},
        fail_agent_request=True,
        fail_code=fail_code,
    )
    harness = CodexHarness(_StubSupervisor(client), events=object())  # type: ignore[arg-type]

    catalog = await harness.model_catalog(Path("."))

    assert client.calls == [{"agent": "codex"}, {}]
    assert [model.id for model in catalog.models] == ["gpt-5.3-codex-spark"]


@pytest.mark.asyncio
async def test_model_catalog_raises_non_param_errors() -> None:
    client = _StubClient(
        response={"data": []},
        fail_agent_request=True,
        fail_code=-32001,
    )
    harness = CodexHarness(_StubSupervisor(client), events=object())  # type: ignore[arg-type]

    with pytest.raises(CodexAppServerResponseError):
        await harness.model_catalog(Path("."))

    assert client.calls == [{"agent": "codex"}]
