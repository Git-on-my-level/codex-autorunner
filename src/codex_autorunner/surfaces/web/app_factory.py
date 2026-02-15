import os
from typing import Mapping, Optional

from fastapi.staticfiles import StaticFiles

from ...core.config import _is_loopback_host


def resolve_auth_token(
    env_name: str, *, env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    if not env_name:
        return None
    source = env if env is not None else os.environ
    value = source.get(env_name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def resolve_allowed_hosts(host: str, allowed_hosts: list[str]) -> list[str]:
    cleaned = [entry.strip() for entry in allowed_hosts if entry and entry.strip()]
    if cleaned:
        return cleaned
    if _is_loopback_host(host):
        return ["localhost", "127.0.0.1", "::1", "testserver"]
    return []


_STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"


class CacheStaticFiles(StaticFiles):
    def __init__(self, *args, cache_control: str = _STATIC_CACHE_CONTROL, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache_control = cache_control

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206, 304):
            response.headers.setdefault("Cache-Control", self._cache_control)
        return response
