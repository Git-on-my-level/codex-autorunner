from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .constants import DISCORD_API_BASE_URL
from .errors import DiscordAPIError


class DiscordRestClient:
    def __init__(
        self,
        *,
        bot_token: str,
        timeout_seconds: float = 10.0,
        base_url: str = DISCORD_API_BASE_URL,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds)
        self._authorization_header = f"Bot {bot_token}"
        self._max_rate_limit_retries = 3

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "DiscordRestClient":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | list[dict[str, Any]] | None = None,
        expect_json: bool = True,
    ) -> Any:
        retries = 0
        while True:
            response = await self._client.request(
                method,
                path,
                json=payload,
                headers={"Authorization": self._authorization_header},
            )
            if response.status_code == 429:
                retry_after_raw = response.headers.get("Retry-After")
                if (
                    retry_after_raw is not None
                    and retries < self._max_rate_limit_retries
                ):
                    retries += 1
                    try:
                        retry_after = max(float(retry_after_raw), 0.0)
                    except ValueError:
                        retry_after = 0.0
                    await asyncio.sleep(retry_after)
                    continue

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

            body_preview = (response.text or "").strip().replace("\n", " ")[:200]
            raise DiscordAPIError(
                f"Discord API request failed for {method} {path}: "
                f"status={response.status_code} body={body_preview!r}"
            )

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
