import os
from typing import Mapping, Optional

from fastapi.staticfiles import StaticFiles

from ...core.config_validation import is_loopback_host


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
    if is_loopback_host(host):
        return ["localhost", "127.0.0.1", "::1", "testserver"]
    return []


_STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
# ES module entrypoints load with ?v= (asset version), but relative imports (./foo.js) resolve
# to URLs without that query (URL spec strips search for relative resolution). Those stable URLs
# must not be cached across navigations or browsers can combine stale and fresh chunks after deploy,
# which surfaces as missing named exports between generated modules.
_GENERATED_JS_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"


def _is_generated_javascript(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return bool(
        normalized.endswith(".js")
        and (
            normalized.startswith("generated/") or normalized.startswith("/generated/")
        )
    )


class CacheStaticFiles(StaticFiles):
    def __init__(self, *args, cache_control: str = _STATIC_CACHE_CONTROL, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache_control = cache_control

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206, 304):
            if _is_generated_javascript(path):
                response.headers["Cache-Control"] = _GENERATED_JS_CACHE_CONTROL
            else:
                response.headers.setdefault("Cache-Control", self._cache_control)
        return response
