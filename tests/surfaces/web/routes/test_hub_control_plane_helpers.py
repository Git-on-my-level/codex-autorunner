from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from codex_autorunner.core.hub_control_plane import HubControlPlaneError
from codex_autorunner.surfaces.web.routes.hub_control_plane import (
    _error_response,
    _require_control_plane_service,
    _run_control_plane_call,
    _run_control_plane_command,
    _status_code_for_error,
)


class TestStatusCodeForError:
    @pytest.mark.parametrize(
        "code,expected_status",
        [
            ("hub_unavailable", 503),
            ("hub_incompatible", 409),
            ("hub_rejected", 400),
            ("transport_failure", 502),
            ("unknown_error", 500),
        ],
    )
    def test_maps_error_codes_to_http_status(
        self, code: str, expected_status: int
    ) -> None:
        exc = HubControlPlaneError(code, "test message")
        assert _status_code_for_error(exc) == expected_status


class TestErrorResponse:
    def test_includes_error_dict_with_code_and_message(self) -> None:
        exc = HubControlPlaneError("hub_rejected", "bad input")
        response = _error_response(exc)
        assert response.status_code == 400
        body = response.body
        import json

        data = json.loads(body)
        assert data["error"]["code"] == "hub_rejected"
        assert data["error"]["message"] == "bad input"
        assert "retryable" in data["error"]


class TestRequireControlPlaneService:
    def test_missing_service_raises_503(self) -> None:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        with pytest.raises(HTTPException) as exc_info:
            _require_control_plane_service(request)
        assert exc_info.value.status_code == 503

    def test_wrong_type_raises_503(self) -> None:
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(hub_control_plane_service="not-a-service")
            )
        )
        with pytest.raises(HTTPException) as exc_info:
            _require_control_plane_service(request)
        assert exc_info.value.status_code == 503


class TestRunControlPlaneCall:
    @pytest.mark.anyio
    async def test_factory_valueerror_becomes_hub_rejected(self) -> None:
        def bad_factory():
            raise ValueError("missing field")

        response = await _run_control_plane_call(
            request_factory=bad_factory,
            operation=lambda r: r,
        )
        assert response.status_code == 400
        import json

        data = json.loads(response.body)
        assert data["error"]["code"] == "hub_rejected"

    @pytest.mark.anyio
    async def test_operation_hub_control_plane_error_maps_status(self) -> None:
        response = await _run_control_plane_call(
            request_factory=lambda: "req",
            operation=lambda r: (_ for _ in ()).throw(
                HubControlPlaneError("hub_unavailable", "down")
            ),
        )
        assert response.status_code == 503

    @pytest.mark.anyio
    async def test_operation_valueerror_becomes_hub_rejected(self) -> None:
        response = await _run_control_plane_call(
            request_factory=lambda: "req",
            operation=lambda r: (_ for _ in ()).throw(ValueError("invalid payload")),
        )
        assert response.status_code == 400

    @pytest.mark.anyio
    async def test_success_returns_json_response(self) -> None:
        class FakeResponse:
            def to_dict(self) -> dict[str, Any]:
                return {"result": "ok"}

        response = await _run_control_plane_call(
            request_factory=lambda: "req",
            operation=lambda r: FakeResponse(),
        )
        assert response.status_code == 200
        import json

        data = json.loads(response.body)
        assert data["result"] == "ok"


class TestRunControlPlaneCommand:
    @pytest.mark.anyio
    async def test_factory_valueerror_becomes_hub_rejected(self) -> None:
        def bad_factory():
            raise ValueError("missing field")

        response = await _run_control_plane_command(
            request_factory=bad_factory,
            operation=lambda r: None,
        )
        assert response.status_code == 400

    @pytest.mark.anyio
    async def test_success_returns_204(self) -> None:
        response = await _run_control_plane_command(
            request_factory=lambda: "req",
            operation=lambda r: None,
        )
        assert response.status_code == 204

    @pytest.mark.anyio
    async def test_operation_hub_control_plane_error_maps_status(self) -> None:
        response = await _run_control_plane_command(
            request_factory=lambda: "req",
            operation=lambda r: (_ for _ in ()).throw(
                HubControlPlaneError("hub_incompatible", "version mismatch")
            ),
        )
        assert response.status_code == 409

    @pytest.mark.anyio
    async def test_operation_valueerror_becomes_hub_rejected(self) -> None:
        response = await _run_control_plane_command(
            request_factory=lambda: "req",
            operation=lambda r: (_ for _ in ()).throw(ValueError("bad data")),
        )
        assert response.status_code == 400
