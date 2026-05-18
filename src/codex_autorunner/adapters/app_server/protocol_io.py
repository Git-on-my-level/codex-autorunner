from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Optional, Union, cast

from ...core.logging_utils import log_event
from .errors import (
    CodexAppServerDisconnected,
    CodexAppServerProtocolError,
    CodexAppServerResponseError,
)
from .protocol_helpers import normalize_response, normalize_response_result
from .transport import build_message

ProcessGetter = Callable[[], Optional[asyncio.subprocess.Process]]
ParamSummarizer = Callable[[str, Optional[dict[str, Any]]], dict[str, Any]]


class AppServerProtocolIO:
    """Own JSON-RPC message writing and pending request response matching."""

    def __init__(
        self,
        *,
        process_getter: ProcessGetter,
        request_timeout: Optional[float],
        logger: logging.Logger,
        summarize_params: ParamSummarizer,
    ) -> None:
        self._process_getter = process_getter
        self._request_timeout = request_timeout
        self._logger = logger
        self._summarize_params = summarize_params
        self._write_lock: Optional[asyncio.Lock] = None
        self._data_lock: Optional[asyncio.Lock] = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._pending_methods: dict[str, str] = {}
        self._next_id: str = str(uuid.uuid4())

    @property
    def pending_request_count(self) -> int:
        return len(self._pending)

    async def request(
        self,
        method: str,
        params: Optional[dict[str, Any]],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        self._ensure_locks()
        data_lock = self._data_lock
        if data_lock is None:
            raise CodexAppServerProtocolError("data lock unavailable")
        request_id = self._next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        async with data_lock:
            self._pending[request_id] = future
            self._pending_methods[request_id] = method
        log_event(
            self._logger,
            logging.INFO,
            "app_server.request",
            request_id=request_id,
            method=method,
            **self._summarize_params(method, params),
        )
        await self.send_message(
            self.build_message(method, params=params, req_id=request_id)
        )
        timeout = timeout if timeout is not None else self._request_timeout
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            if not future.done():
                future.cancel()
            raise
        finally:
            async with data_lock:
                self._pending.pop(request_id, None)
                self._pending_methods.pop(request_id, None)

    async def notify(self, method: str, params: Optional[dict[str, Any]]) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "app_server.notify",
            method=method,
            **self._summarize_params(method, params),
        )
        await self.send_message(self.build_message(method, params=params))

    async def send_message(self, message: dict[str, Any]) -> None:
        process = self._process_getter()
        if not process or not process.stdin:
            raise CodexAppServerDisconnected("App-server process is not running")
        self._ensure_locks()
        write_lock = self._write_lock
        if write_lock is None:
            raise CodexAppServerProtocolError("write lock unavailable")
        payload = json.dumps(message, separators=(",", ":"))
        async with write_lock:
            process.stdin.write((payload + "\n").encode("utf-8"))
            await process.stdin.drain()

    async def handle_response(self, message: dict[str, Any]) -> None:
        normalized = normalize_response(message)
        if normalized is None:
            return

        req_id = normalized.request_id
        self._ensure_locks()
        data_lock = self._data_lock
        if data_lock is None:
            raise CodexAppServerProtocolError("data lock unavailable")
        async with data_lock:
            future = self._pending.pop(req_id, None)
            method = self._pending_methods.pop(req_id, None)
        if future is None:
            log_event(
                self._logger,
                logging.DEBUG,
                "app_server.response.unmatched",
                request_id=req_id,
                request_id_type=type(req_id).__name__,
                method=method,
            )
            return
        if future.cancelled():
            return
        result = normalize_response_result(normalized)
        if result.is_error:
            if result.error_code == -32600:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.response.invalid_request",
                    request_id=req_id,
                    request_id_type=type(req_id).__name__,
                    method=method,
                    error_code=result.error_code,
                    error_message=result.error_message,
                )
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.response.error",
                request_id=req_id,
                request_id_type=type(req_id).__name__,
                method=method,
                error_code=result.error_code,
                error_message=result.error_message,
            )
            future.set_exception(
                CodexAppServerResponseError(
                    method=method,
                    code=result.error_code,
                    message=result.error_message or "app-server error",
                    data=result.error_data,
                )
            )
            return
        log_event(
            self._logger,
            logging.INFO,
            "app_server.response",
            request_id=req_id,
            request_id_type=type(req_id).__name__,
            method=method,
        )
        future.set_result(result.result)

    def fail_pending_requests(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self._pending_methods.clear()

    def build_message(
        self,
        method: Optional[str] = None,
        *,
        params: Optional[dict[str, Any]] = None,
        req_id: Optional[Union[int, str]] = None,
        result: Optional[Any] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            build_message(
                method,
                params=params,
                req_id=req_id,
                result=result,
                error=error,
            ),
        )

    def _ensure_locks(self) -> None:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        if self._data_lock is None:
            self._data_lock = asyncio.Lock()

    def _next_request_id(self) -> str:
        self._next_id = str(uuid.uuid4())
        return self._next_id
