from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from typing import Any, AsyncIterator, Iterable, Optional

import httpx

from ...core.logging_utils import log_event
from .events import SSEEvent, parse_sse_lines

_MAX_INVALID_JSON_PREVIEW_BYTES = 512


@dataclasses.dataclass
class OpenCodeApiProfile:
    """Detected OpenCode API capabilities from OpenAPI spec."""

    supports_prompt_async: bool = True
    supports_global_endpoints: bool = True
    spec_fetched: bool = False


class OpenCodeProtocolError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        content_type: Optional[str] = None,
        body_preview: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.content_type = content_type
        self.body_preview = body_preview


def _normalize_sse_event(event: SSEEvent) -> SSEEvent:
    event_type = event.event
    raw_data = event.data or ""
    payload_obj: Optional[dict[str, Any]] = None
    try:
        payload_obj = json.loads(raw_data) if raw_data else None
    except (json.JSONDecodeError, TypeError):
        payload_obj = None

    if isinstance(payload_obj, dict) and isinstance(payload_obj.get("payload"), dict):
        outer = payload_obj
        inner = dict(outer.get("payload") or {})
        if "type" not in inner and isinstance(outer.get("type"), str):
            inner["type"] = outer["type"]
        for key in ("sessionID", "sessionId", "session_id"):
            if key in outer and key not in inner:
                inner[key] = outer[key]
        if "session" in outer and "session" not in inner:
            inner["session"] = outer["session"]
        if "properties" in outer and "properties" not in inner:
            inner["properties"] = outer["properties"]
        payload_obj = inner

    if isinstance(payload_obj, dict):
        payload_type = payload_obj.get("type")
        if isinstance(payload_type, str) and payload_type:
            event_type = payload_type
        raw_data = json.dumps(payload_obj)

    return SSEEvent(
        event=event_type,
        data=raw_data,
        id=event.id,
        retry=event.retry,
    )


class OpenCodeClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth: Optional[tuple[str, str]] = None,
        timeout: Optional[float] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
        )
        self._logger = logger or logging.getLogger(__name__)
        self._api_profile: Optional[OpenCodeApiProfile] = None
        self._api_profile_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def detect_api_shape(self) -> OpenCodeApiProfile:
        """Detect OpenCode API capabilities by fetching and parsing OpenAPI spec.
        Results are cached for the lifetime of the client instance.
        Thread-safe: multiple concurrent calls will wait for first detection to complete.
        """
        async with self._api_profile_lock:
            if self._api_profile is not None:
                return self._api_profile

            profile = OpenCodeApiProfile()
            try:
                spec = await self.fetch_openapi_spec()
                profile.spec_fetched = True

                if isinstance(spec, dict):
                    # Check if /session/{id}/prompt_async exists
                    profile.supports_prompt_async = self.has_endpoint(
                        spec, "post", "/session/{session_id}/prompt_async"
                    )

                    # Check if /global/* endpoints exist
                    profile.supports_global_endpoints = self.has_endpoint(
                        spec, "get", "/global/health"
                    ) or self.has_endpoint(spec, "get", "/global/event")

                log_event(
                    self._logger,
                    logging.INFO,
                    "opencode.api_shape_detected",
                    supports_prompt_async=profile.supports_prompt_async,
                    supports_global_endpoints=profile.supports_global_endpoints,
                )
            except Exception as exc:
                self._logger.warning(
                    "Failed to detect API shape, assuming modern OpenCode: %s", exc
                )
                # Default to assuming modern OpenCode with all features
                profile.supports_prompt_async = True
                profile.supports_global_endpoints = True

            self._api_profile = profile
            return profile

    def _get_api_profile(self) -> OpenCodeApiProfile:
        """Get API profile, detecting if needed. Synchronous for use in sync methods."""
        if self._api_profile is None:
            # Return default profile if not yet detected
            return OpenCodeApiProfile()
        return self._api_profile

    def _dir_params(self, directory: Optional[str]) -> dict[str, str]:
        return {"directory": directory} if directory else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        expect_json: bool = True,
    ) -> Any:
        response = await self._client.request(
            method, path, params=params, json=json_body
        )
        response.raise_for_status()
        raw = response.content
        if not raw or not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            self._log_invalid_json(
                method,
                path,
                response,
                raw,
                expect_json=expect_json,
            )
            if expect_json:
                preview = (
                    raw[:_MAX_INVALID_JSON_PREVIEW_BYTES]
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                content_type = response.headers.get("content-type")
                hint = ""
                if content_type and "text/html" in content_type.lower():
                    hint = (
                        " Response looks like HTML; the OpenCode server may have "
                        "proxied the request instead of handling an API route."
                    )
                elif preview.startswith("<"):
                    hint = (
                        " Response looks like HTML; check that the OpenCode API "
                        "endpoint is correct."
                    )
                raise OpenCodeProtocolError(
                    f"OpenCode returned invalid JSON.{hint}",
                    status_code=response.status_code,
                    content_type=content_type,
                    body_preview=preview or None,
                ) from exc
        return None

    def _log_invalid_json(
        self,
        method: str,
        path: str,
        response: httpx.Response,
        raw: bytes,
        *,
        expect_json: bool,
    ) -> None:
        preview = raw[:_MAX_INVALID_JSON_PREVIEW_BYTES].decode(
            "utf-8", errors="replace"
        )
        log_event(
            self._logger,
            logging.WARNING,
            "opencode.response.invalid_json",
            method=method,
            path=path,
            status_code=response.status_code,
            content_length=len(raw),
            content_type=response.headers.get("content-type"),
            expect_json=expect_json,
            preview=preview,
        )

    async def providers(self, directory: Optional[str] = None) -> Any:
        return await self._request(
            "GET",
            "/config/providers",
            params=self._dir_params(directory),
            expect_json=True,
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
        return await self._request(
            "POST", "/session", json_body=payload, expect_json=True
        )

    async def list_sessions(self, directory: Optional[str] = None) -> Any:
        return await self._request(
            "GET", "/session", params=self._dir_params(directory), expect_json=True
        )

    async def get_session(self, session_id: str) -> Any:
        return await self._request("GET", f"/session/{session_id}", expect_json=True)

    async def session_status(self, *, directory: Optional[str] = None) -> Any:
        return await self._request(
            "GET",
            "/session/status",
            params=self._dir_params(directory),
            expect_json=True,
        )

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
            "POST",
            f"/session/{session_id}/message",
            json_body=payload,
            expect_json=False,
        )

    async def prompt(
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

        profile = await self.detect_api_shape()
        if profile.supports_prompt_async:
            return await self._request(
                "POST",
                f"/session/{session_id}/prompt_async",
                json_body=payload,
                expect_json=False,
            )
        else:
            return await self._request(
                "POST",
                f"/session/{session_id}/message",
                json_body=payload,
                expect_json=True,
            )

    async def prompt_async(
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

        profile = await self.detect_api_shape()
        if profile.supports_prompt_async:
            return await self._request(
                "POST",
                f"/session/{session_id}/prompt_async",
                json_body=payload,
                expect_json=False,
            )
        else:
            return await self._request(
                "POST",
                f"/session/{session_id}/message",
                json_body=payload,
                expect_json=True,
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
            "POST",
            f"/session/{session_id}/command",
            json_body=payload,
            expect_json=False,
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
            "POST",
            f"/session/{session_id}/summarize",
            json_body=payload,
            expect_json=True,
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
            "POST",
            f"/permission/{request_id}/reply",
            json_body=payload,
            expect_json=False,
        )

    async def list_questions(self) -> Any:
        return await self._request("GET", "/question", expect_json=True)

    async def reply_question(self, request_id: str, *, answers: list[list[str]]) -> Any:
        payload: dict[str, Any] = {"answers": answers}
        return await self._request(
            "POST",
            f"/question/{request_id}/reply",
            json_body=payload,
            expect_json=False,
        )

    async def reject_question(self, request_id: str) -> Any:
        return await self._request(
            "POST",
            f"/question/{request_id}/reject",
            expect_json=False,
        )

    async def abort(self, session_id: str) -> Any:
        return await self._request(
            "POST", f"/session/{session_id}/abort", expect_json=False
        )

    async def health(self) -> Any:
        """Check OpenCode server health using /global/health or /health endpoint."""
        profile = await self.detect_api_shape()
        if profile.supports_global_endpoints:
            return await self._request("GET", "/global/health", expect_json=True)
        else:
            return await self._request("GET", "/health", expect_json=True)

    async def dispose(self, session_id: str) -> Any:
        """Dispose of a session using /global/dispose/{id} or /session/{id}/dispose endpoint."""
        profile = await self.detect_api_shape()
        if profile.supports_global_endpoints:
            return await self._request(
                "POST", f"/global/dispose/{session_id}", expect_json=False
            )
        else:
            return await self._request(
                "POST", f"/session/{session_id}/dispose", expect_json=False
            )

    async def stream_events(
        self,
        *,
        directory: Optional[str] = None,
        ready_event: Optional[asyncio.Event] = None,
        paths: Optional[Iterable[str]] = None,
    ) -> AsyncIterator[SSEEvent]:
        params = self._dir_params(directory)

        if paths is not None:
            event_paths = list(paths)
        else:
            profile = await self.detect_api_shape()
            if profile.supports_global_endpoints:
                event_paths = (
                    ["/event", "/global/event"]
                    if directory
                    else ["/global/event", "/event"]
                )
            else:
                event_paths = ["/event"]

        last_error: Optional[BaseException] = None
        for path in event_paths:
            try:
                async with self._client.stream(
                    "GET", path, params=params, timeout=None
                ) as response:
                    response.raise_for_status()
                    if ready_event is not None:
                        ready_event.set()
                    async for sse in parse_sse_lines(response.aiter_lines()):
                        yield _normalize_sse_event(sse)
                return
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = exc.response.status_code
                if status_code in (404, 405):
                    continue
                raise
            except Exception as exc:
                last_error = exc
                raise
        if ready_event is not None and not ready_event.is_set():
            ready_event.set()
        if last_error is not None:
            raise last_error

    async def fetch_openapi_spec(self) -> dict[str, Any]:
        """Fetch OpenAPI spec from /doc endpoint for capability negotiation."""
        response = await self._client.get("/doc")
        response.raise_for_status()
        content = response.content
        try:
            spec = json.loads(content) if content else {}
            log_event(
                self._logger,
                logging.INFO,
                "opencode.openapi.fetched",
                paths=len(spec.get("paths", {})) if isinstance(spec, dict) else 0,
                has_components=(
                    "components" in spec if isinstance(spec, dict) else False
                ),
            )
            return spec
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "opencode.openapi.parse_failed",
                exc=exc,
            )
            raise OpenCodeProtocolError(
                f"Failed to parse OpenAPI spec: {exc}",
                status_code=response.status_code,
                content_type=(
                    response.headers.get("content-type") if response else None
                ),
            ) from exc

    def has_endpoint(
        self, openapi_spec: dict[str, Any], method: str, path: str
    ) -> bool:
        """Check if endpoint is available in OpenAPI spec.

        The OpenAPI spec sometimes uses different template parameter names (e.g.,
        `{sessionID}` vs `{session_id}`). We normalize templates before matching so
        capability detection does not depend on placeholder spelling.
        """
        if not isinstance(openapi_spec, dict):
            return False
        paths = openapi_spec.get("paths", {})
        if not isinstance(paths, dict):
            return False

        target = _normalize_template_path(path)
        method = method.lower()

        for candidate_path, info in paths.items():
            if not isinstance(info, dict):
                continue
            if _normalize_template_path(candidate_path) != target:
                continue
            if method in info:
                return True
        return False


def _normalize_template_path(path: str) -> str:
    """Collapse template placeholders to a canonical form.

    Example: `/session/{sessionID}/prompt_async` -> `/session/{}/prompt_async`
    """
    return re.sub(r"{[^/]+}", "{}", path)


__all__ = ["OpenCodeClient", "OpenCodeProtocolError", "OpenCodeApiProfile"]
