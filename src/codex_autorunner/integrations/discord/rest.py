from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any, AsyncIterator, Optional

import httpx

from codex_autorunner.core.circuit_breaker import CircuitBreaker
from codex_autorunner.core.exceptions import CircuitOpenError

from .constants import DISCORD_API_BASE_URL
from .errors import DiscordAPIError, DiscordPermanentError

logger = logging.getLogger(__name__)


class DiscordRestClient:
    def __init__(
        self,
        *,
        bot_token: str,
        timeout_seconds: float = 10.0,
        base_url: str = DISCORD_API_BASE_URL,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds)
        self._authorization_header = f"Bot {bot_token}"
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._circuit_breakers: dict[str, CircuitBreaker] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "DiscordRestClient":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    def _circuit_breaker_scope(self, path: str) -> str:
        return path.split("/")[1] if "/" in path else "default"

    @asynccontextmanager
    async def _resilience_guard(self, path: str) -> AsyncIterator[None]:
        scope = self._circuit_breaker_scope(path)
        breaker = self._circuit_breakers.get(scope)
        if breaker is None:
            breaker = CircuitBreaker(f"Discord:{scope}", logger=logger)
            self._circuit_breakers[scope] = breaker
        async with breaker.call():
            yield

    def _calculate_retry_delay(self, attempt: int) -> float:
        delay = self._retry_base_delay * (2**attempt) + random.uniform(0, 1)
        return float(min(delay, self._retry_max_delay))

    def _is_retryable_error(self, exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
            ),
        ):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return 500 <= exc.response.status_code < 600
        return False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | list[dict[str, Any]] | None = None,
        expect_json: bool = True,
    ) -> Any:
        rate_limit_retries = 0
        retry_attempt = 0

        while True:
            try:
                async with self._resilience_guard(path):
                    response = await self._client.request(
                        method,
                        path,
                        json=payload,
                        headers={"Authorization": self._authorization_header},
                    )
                    response.raise_for_status()
            except CircuitOpenError:
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    retry_after_raw = exc.response.headers.get("Retry-After")
                    if (
                        retry_after_raw is not None
                        and rate_limit_retries < self._max_retries
                    ):
                        rate_limit_retries += 1
                        try:
                            retry_after = max(float(retry_after_raw), 0.0)
                        except ValueError:
                            retry_after = 0.0
                        logger.info(
                            "Discord rate limited on %s %s, retrying after %.1fs (attempt %d)",
                            method,
                            path,
                            retry_after,
                            rate_limit_retries,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    raise DiscordAPIError(
                        f"Discord API rate limit exceeded for {method} {path}"
                    ) from exc

                status_code = exc.response.status_code
                body_preview = (
                    (exc.response.text or "").strip().replace("\n", " ")[:200]
                )

                if 200 <= status_code < 300:
                    response = exc.response
                elif 500 <= status_code < 600:
                    if retry_attempt < self._max_retries:
                        retry_attempt += 1
                        delay = self._calculate_retry_delay(retry_attempt)
                        logger.warning(
                            "Discord server error %d on %s %s, retrying in %.1fs (attempt %d/%d)",
                            exc.response.status_code,
                            method,
                            path,
                            delay,
                            retry_attempt,
                            self._max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise DiscordAPIError(
                        f"Discord API server error for {method} {path}: "
                        f"status={status_code} body={body_preview!r}"
                    ) from exc
                elif status_code in {401, 403}:
                    raise DiscordPermanentError(
                        f"Discord API authentication failure for {method} {path}: "
                        f"status={status_code} body={body_preview!r}"
                    ) from exc
                else:
                    raise DiscordAPIError(
                        f"Discord API request failed for {method} {path}: "
                        f"status={status_code} body={body_preview!r}"
                    ) from exc
            except httpx.HTTPError as exc:
                if self._is_retryable_error(exc) and retry_attempt < self._max_retries:
                    retry_attempt += 1
                    delay = self._calculate_retry_delay(retry_attempt)
                    logger.warning(
                        "Discord network error on %s %s: %s, retrying in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        type(exc).__name__,
                        delay,
                        retry_attempt,
                        self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise DiscordAPIError(
                    f"Discord API network error for {method} {path}: {exc}"
                ) from exc

            if 200 <= response.status_code < 300:
                if not expect_json:
                    return None
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise DiscordAPIError(
                        f"Discord API returned non-JSON success response for {method} {path}"
                    ) from exc

    async def get_gateway_bot(self) -> dict[str, Any]:
        payload = await self._request("GET", "/gateway/bot")
        return payload if isinstance(payload, dict) else {}

    async def list_application_commands(
        self, *, application_id: str, guild_id: str | None = None
    ) -> list[dict[str, Any]]:
        path = (
            f"/applications/{application_id}/commands"
            if guild_id is None
            else f"/applications/{application_id}/guilds/{guild_id}/commands"
        )
        payload = await self._request("GET", path)
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: str | None = None,
    ) -> list[dict[str, Any]]:
        path = (
            f"/applications/{application_id}/commands"
            if guild_id is None
            else f"/applications/{application_id}/guilds/{guild_id}/commands"
        )
        payload = await self._request("PUT", path, payload=commands)
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        await self._request(
            "POST",
            f"/interactions/{interaction_id}/{interaction_token}/callback",
            payload=payload,
            expect_json=False,
        )

    async def create_followup_message(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/webhooks/{application_id}/{interaction_token}",
            payload=payload,
        )
        return response if isinstance(response, dict) else {}

    async def create_channel_message(
        self,
        *,
        channel_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/channels/{channel_id}/messages",
            payload=payload,
        )
        return response if isinstance(response, dict) else {}

    async def create_channel_message_with_attachment(
        self,
        *,
        channel_id: str,
        data: bytes,
        filename: str,
        caption: Optional[str] = None,
    ) -> dict[str, Any]:
        form_data = {
            "files[0]": (filename, BytesIO(data)),
        }
        payload: dict[str, Any] = {}
        if caption:
            payload["content"] = caption
        if payload:
            import json

            form_data["payload_json"] = json.dumps(payload)
        return await self._upload_multipart(
            f"/channels/{channel_id}/messages",
            form_data,
        )

    async def _upload_multipart(
        self,
        path: str,
        form_data: dict[str, Any],
    ) -> Any:
        rate_limit_retries = 0
        retry_attempt = 0

        while True:
            try:
                async with self._resilience_guard(path):
                    files: list[tuple[str, tuple[str, Any, Optional[str]]]] = []
                    data_fields: dict[str, str] = {}
                    for key, value in form_data.items():
                        if isinstance(value, tuple) and len(value) >= 2:
                            file_name, file_obj = value[0], value[1]
                            content_type = value[2] if len(value) > 2 else None
                            files.append((key, (file_name, file_obj, content_type)))
                        else:
                            data_fields[key] = value

                    response = await self._client.request(
                        "POST",
                        path,
                        files=files if files else None,
                        data=data_fields if data_fields else None,
                        headers={"Authorization": self._authorization_header},
                    )
                    response.raise_for_status()
            except CircuitOpenError:
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    retry_after_raw = exc.response.headers.get("Retry-After")
                    if (
                        retry_after_raw is not None
                        and rate_limit_retries < self._max_retries
                    ):
                        rate_limit_retries += 1
                        try:
                            retry_after = max(float(retry_after_raw), 0.0)
                        except ValueError:
                            retry_after = 0.0
                        logger.info(
                            "Discord rate limited on multipart %s, retrying after %.1fs (attempt %d)",
                            path,
                            retry_after,
                            rate_limit_retries,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    raise DiscordAPIError(
                        f"Discord API rate limit exceeded for multipart {path}"
                    ) from exc

                if 200 <= exc.response.status_code < 300:
                    response = exc.response
                elif 500 <= exc.response.status_code < 600:
                    if retry_attempt < self._max_retries:
                        retry_attempt += 1
                        delay = self._calculate_retry_delay(retry_attempt)
                        logger.warning(
                            "Discord server error %d on multipart %s, retrying in %.1fs (attempt %d/%d)",
                            exc.response.status_code,
                            path,
                            delay,
                            retry_attempt,
                            self._max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    body_preview = (
                        (exc.response.text or "").strip().replace("\n", " ")[:200]
                    )
                    raise DiscordAPIError(
                        f"Discord API server error for multipart {path}: "
                        f"status={exc.response.status_code} body={body_preview!r}"
                    ) from exc
                else:
                    body_preview = (
                        (exc.response.text or "").strip().replace("\n", " ")[:200]
                    )
                    raise DiscordAPIError(
                        f"Discord API request failed for multipart {path}: "
                        f"status={exc.response.status_code} body={body_preview!r}"
                    ) from exc
            except httpx.HTTPError as exc:
                if self._is_retryable_error(exc) and retry_attempt < self._max_retries:
                    retry_attempt += 1
                    delay = self._calculate_retry_delay(retry_attempt)
                    logger.warning(
                        "Discord network error on multipart %s: %s, retrying in %.1fs (attempt %d/%d)",
                        path,
                        type(exc).__name__,
                        delay,
                        retry_attempt,
                        self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise DiscordAPIError(
                    f"Discord API network error for multipart {path}: {exc}"
                ) from exc

            if 200 <= response.status_code < 300:
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise DiscordAPIError(
                        f"Discord API returned non-JSON success response for multipart {path}"
                    ) from exc
            return {}

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "PATCH",
            f"/webhooks/{application_id}/{interaction_token}/messages/@original",
            payload=payload,
        )
        return response if isinstance(response, dict) else {}

    async def edit_channel_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            payload=payload,
        )
        return response if isinstance(response, dict) else {}

    async def delete_channel_message(
        self,
        *,
        channel_id: str,
        message_id: str,
    ) -> None:
        await self._request(
            "DELETE",
            f"/channels/{channel_id}/messages/{message_id}",
            expect_json=False,
        )
