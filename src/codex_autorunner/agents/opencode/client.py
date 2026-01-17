from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

import httpx

from .events import SSEEvent, parse_sse_lines


class OpenCodeClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth: Optional[tuple[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _dir_params(self, directory: Optional[str]) -> dict[str, str]:
        return {"directory": directory} if directory else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> Any:
        response = await self._client.request(method, path, params=params, json=json)
        response.raise_for_status()
        if response.content:
            return response.json()
        return None

    async def providers(self, directory: Optional[str] = None) -> Any:
        return await self._request(
            "GET",
            "/config/providers",
            params=self._dir_params(directory),
        )

    async def create_session(
        self,
        *,
        title: Optional[str] = None,
        directory: Optional[str] = None,
    ) -> Any:
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if directory:
            payload["directory"] = directory
        return await self._request("POST", "/session", json=payload)

    async def list_sessions(self, directory: Optional[str] = None) -> Any:
        return await self._request(
            "GET", "/session", params=self._dir_params(directory)
        )

    async def get_session(self, session_id: str) -> Any:
        return await self._request("GET", f"/session/{session_id}")

    async def send_message(
        self,
        session_id: str,
        *,
        message: str,
        agent: Optional[str] = None,
        model: Optional[dict[str, str]] = None,
        variant: Optional[str] = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": message}],
        }
        if agent:
            payload["agent"] = agent
        if model:
            payload["model"] = model
        if variant:
            payload["variant"] = variant
        return await self._request(
            "POST", f"/session/{session_id}/message", json=payload
        )

    async def send_command(
        self,
        session_id: str,
        *,
        command: str,
        arguments: Optional[str] = None,
        model: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "command": command,
            "arguments": arguments or "",
        }
        if model:
            payload["model"] = model
        if agent:
            payload["agent"] = agent
        return await self._request(
            "POST", f"/session/{session_id}/command", json=payload
        )

    async def summarize(
        self,
        session_id: str,
        *,
        provider_id: str,
        model_id: str,
        auto: Optional[bool] = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "providerID": provider_id,
            "modelID": model_id,
        }
        if auto is not None:
            payload["auto"] = auto
        return await self._request(
            "POST", f"/session/{session_id}/summarize", json=payload
        )

    async def respond_permission(
        self,
        *,
        request_id: str,
        reply: str,
        message: Optional[str] = None,
    ) -> Any:
        payload: dict[str, Any] = {"reply": reply}
        if message:
            payload["message"] = message
        return await self._request(
            "POST", f"/permission/{request_id}/reply", json=payload
        )

    async def abort(self, session_id: str) -> Any:
        return await self._request("POST", f"/session/{session_id}/abort")

    async def stream_events(
        self, *, directory: Optional[str] = None
    ) -> AsyncIterator[SSEEvent]:
        params = self._dir_params(directory)
        async with self._client.stream("GET", "/event", params=params) as response:
            response.raise_for_status()
            async for sse in parse_sse_lines(response.aiter_lines()):
                event_type = sse.event
                try:
                    payload = json.loads(sse.data) if sse.data else None
                    if isinstance(payload, dict) and "type" in payload:
                        event_type = str(payload["type"])
                except (json.JSONDecodeError, TypeError):
                    pass
                yield SSEEvent(
                    event=event_type,
                    data=sse.data,
                    id=sse.id,
                    retry=sse.retry,
                )


__all__ = ["OpenCodeClient"]
