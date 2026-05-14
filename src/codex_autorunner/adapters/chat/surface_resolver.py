from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from ..discord.config import DEFAULT_BOT_TOKEN_ENV as DEFAULT_DISCORD_BOT_TOKEN_ENV
from ..discord.rest import DiscordRestClient
from ..telegram.client import TelegramBotClient

DEFAULT_TELEGRAM_BOT_TOKEN_ENV = "CAR_TELEGRAM_BOT_TOKEN"

_DISCORD_CHANNEL_TYPES = {
    0: "text",
    1: "dm",
    2: "voice",
    3: "group_dm",
    4: "category",
    5: "announcement",
    10: "announcement_thread",
    11: "public_thread",
    12: "private_thread",
    13: "stage_voice",
    14: "directory",
    15: "forum",
    16: "media",
}


@dataclass(frozen=True)
class SurfaceInfo:
    channel_id: str
    name: str
    surface_type: str
    parent_name: Optional[str] = None
    parent_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "surface_type": self.surface_type,
            "parent_name": self.parent_name,
            "parent_id": self.parent_id,
            "raw": self.raw,
        }


class SurfaceResolver(Protocol):
    async def resolve(self, key: str) -> Optional[SurfaceInfo]: ...


def _normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_discord_channel_key(value: Any) -> bool:
    normalized = _normalize_text(value)
    return normalized is not None and normalized.isdigit()


def _token_env(raw_config: Mapping[str, Any], section_name: str, default: str) -> str:
    section = raw_config.get(section_name)
    if isinstance(section, Mapping):
        configured = _normalize_text(section.get("bot_token_env"))
        if configured:
            return configured
    return default


class DiscordSurfaceResolver:
    def __init__(
        self,
        *,
        bot_token: Optional[str],
        concurrency: int = 5,
        rest_client_factory: Any = None,
    ) -> None:
        self._bot_token = _normalize_text(bot_token)
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self._rest_client_factory = rest_client_factory or DiscordRestClient
        self._channel_cache: dict[str, Optional[SurfaceInfo]] = {}
        self._guild_cache: dict[str, Optional[dict[str, Any]]] = {}
        self._rest_context: Any = None
        self._rest: Any = None
        self._rest_failed = False

    async def _client(self) -> Any:
        if not self._bot_token or self._rest_failed:
            return None
        if self._rest is not None:
            return self._rest
        try:
            self._rest_context = self._rest_client_factory(bot_token=self._bot_token)
            self._rest = await self._rest_context.__aenter__()
        except Exception:
            self._rest_failed = True
            self._rest_context = None
            self._rest = None
        return self._rest

    async def close(self) -> None:
        if self._rest_context is not None:
            await self._rest_context.__aexit__(None, None, None)
        self._rest_context = None
        self._rest = None

    async def __aenter__(self) -> "DiscordSurfaceResolver":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def resolve(self, key: str) -> Optional[SurfaceInfo]:
        channel_id = _normalize_text(key)
        if channel_id is None or not is_discord_channel_key(channel_id):
            return None
        if channel_id in self._channel_cache:
            return self._channel_cache[channel_id]
        client = await self._client()
        if client is None:
            self._channel_cache[channel_id] = None
            return None
        async with self._semaphore:
            try:
                channel_payload = await client.get_channel(channel_id=channel_id)
            except Exception:
                self._channel_cache[channel_id] = None
                return None
        guild_id = _normalize_text(channel_payload.get("guild_id"))
        guild_payload: Optional[dict[str, Any]] = None
        if guild_id:
            guild_payload = await self._resolve_guild(guild_id)
        info = _discord_surface_info(
            channel_id=channel_id,
            channel_payload=channel_payload,
            guild_payload=guild_payload,
        )
        self._channel_cache[channel_id] = info
        return info

    async def _resolve_guild(self, guild_id: str) -> Optional[dict[str, Any]]:
        if guild_id in self._guild_cache:
            return self._guild_cache[guild_id]
        client = await self._client()
        if client is None:
            self._guild_cache[guild_id] = None
            return None
        async with self._semaphore:
            try:
                raw_guild_payload = await client.get_guild(guild_id=guild_id)
                guild_payload = (
                    dict(raw_guild_payload)
                    if isinstance(raw_guild_payload, Mapping)
                    else None
                )
            except Exception:
                guild_payload = None
        self._guild_cache[guild_id] = guild_payload
        return guild_payload


def _discord_surface_info(
    *,
    channel_id: str,
    channel_payload: Mapping[str, Any],
    guild_payload: Optional[Mapping[str, Any]],
) -> SurfaceInfo:
    channel_type = channel_payload.get("type")
    surface_type = (
        _DISCORD_CHANNEL_TYPES.get(channel_type, str(channel_type))
        if isinstance(channel_type, int)
        else "unknown"
    )
    if channel_type == 1:
        name = "(DM)"
    else:
        raw_name = _normalize_text(channel_payload.get("name"))
        if raw_name and not raw_name.startswith("#"):
            name = f"#{raw_name}"
        else:
            name = raw_name or "(unavailable)"
    parent_name = None
    if isinstance(guild_payload, Mapping):
        parent_name = _normalize_text(guild_payload.get("name"))
    return SurfaceInfo(
        channel_id=channel_id,
        name=name,
        surface_type=surface_type,
        parent_name=parent_name,
        parent_id=_normalize_text(channel_payload.get("guild_id")),
        raw={
            "channel": dict(channel_payload),
            "guild": (
                dict(guild_payload) if isinstance(guild_payload, Mapping) else None
            ),
        },
    )


class TelegramSurfaceResolver:
    def __init__(
        self,
        *,
        bot_token: Optional[str],
        client_factory: Any = None,
    ) -> None:
        self._bot_token = _normalize_text(bot_token)
        self._client_factory = client_factory or TelegramBotClient
        self._cache: dict[str, Optional[SurfaceInfo]] = {}

    async def resolve(self, key: str) -> Optional[SurfaceInfo]:
        surface_key = _normalize_text(key)
        if surface_key is None:
            return None
        if surface_key in self._cache:
            return self._cache[surface_key]
        chat_id = _telegram_chat_id_from_key(surface_key)
        if chat_id is None or not self._bot_token:
            self._cache[surface_key] = None
            return None
        try:
            async with self._client_factory(self._bot_token) as client:
                payload = await client.get_chat(chat_id=chat_id)
        except Exception:
            self._cache[surface_key] = None
            return None
        info = _telegram_surface_info(surface_key=surface_key, payload=payload)
        self._cache[surface_key] = info
        return info


def _telegram_chat_id_from_key(surface_key: str) -> Optional[str]:
    head = surface_key.split(":", 1)[0].strip()
    return head or None


def _telegram_surface_info(
    *, surface_key: str, payload: Mapping[str, Any]
) -> SurfaceInfo:
    title = _normalize_text(payload.get("title"))
    username = _normalize_text(payload.get("username"))
    first_name = _normalize_text(payload.get("first_name"))
    last_name = _normalize_text(payload.get("last_name"))
    full_name = " ".join(
        part for part in (first_name, last_name) if isinstance(part, str) and part
    ).strip()
    if title:
        name = title
    elif username:
        name = f"@{username}"
    elif full_name:
        name = full_name
    else:
        name = "(unavailable)"
    return SurfaceInfo(
        channel_id=surface_key,
        name=name,
        surface_type=_normalize_text(payload.get("type")) or "unknown",
        parent_name=None,
        parent_id=None,
        raw=dict(payload),
    )


def build_surface_resolvers(
    raw_config: Mapping[str, Any],
) -> dict[str, SurfaceResolver]:
    discord_token_env = _token_env(
        raw_config, "discord_bot", DEFAULT_DISCORD_BOT_TOKEN_ENV
    )
    telegram_token_env = _token_env(
        raw_config, "telegram_bot", DEFAULT_TELEGRAM_BOT_TOKEN_ENV
    )
    return {
        "discord": DiscordSurfaceResolver(bot_token=os.environ.get(discord_token_env)),
        "telegram": TelegramSurfaceResolver(
            bot_token=os.environ.get(telegram_token_env)
        ),
    }


async def close_surface_resolvers(resolvers: Mapping[str, SurfaceResolver]) -> None:
    for resolver in resolvers.values():
        close = getattr(resolver, "close", None)
        if callable(close):
            await close()


async def resolve_surface_key(
    resolvers: Mapping[str, SurfaceResolver],
    *,
    surface_kind: Any,
    surface_key: Any,
) -> Optional[SurfaceInfo]:
    kind = _normalize_text(surface_kind)
    key = _normalize_text(surface_key)
    if kind is None or key is None:
        return None
    resolver = resolvers.get(kind)
    if resolver is None:
        return None
    try:
        return await resolver.resolve(key)
    except Exception:
        return None


async def resolve_surface_bindings(
    bindings: Sequence[Mapping[str, Any]],
    resolvers: Mapping[str, SurfaceResolver],
) -> dict[tuple[str, str], Optional[SurfaceInfo]]:
    keys = sorted(
        {
            (surface_kind, surface_key)
            for binding in bindings
            for surface_kind, surface_key in [
                (
                    _normalize_text(binding.get("surface_kind")),
                    _normalize_text(binding.get("surface_key")),
                )
            ]
            if surface_kind is not None and surface_key is not None
        }
    )

    async def resolve_one(surface_kind: str, surface_key: str) -> None:
        results[(surface_kind, surface_key)] = await resolve_surface_key(
            resolvers,
            surface_kind=surface_kind,
            surface_key=surface_key,
        )

    results: dict[tuple[str, str], Optional[SurfaceInfo]] = {}
    await asyncio.gather(*(resolve_one(kind, key) for kind, key in keys))
    return results


def surface_info_display(info: Optional[SurfaceInfo]) -> str:
    if info is None:
        return "(unavailable)"
    if info.parent_name:
        return f"{info.parent_name} / {info.name}"
    return info.name


__all__ = [
    "DEFAULT_TELEGRAM_BOT_TOKEN_ENV",
    "DiscordSurfaceResolver",
    "SurfaceInfo",
    "SurfaceResolver",
    "TelegramSurfaceResolver",
    "build_surface_resolvers",
    "close_surface_resolvers",
    "is_discord_channel_key",
    "resolve_surface_bindings",
    "resolve_surface_key",
    "surface_info_display",
]
