from __future__ import annotations

from typing import Optional

from fastapi import Request

from .....agents.hermes.supervisor import build_hermes_supervisor_from_config
from .....core.text_utils import _normalize_optional_text


def resolve_cached_hermes_supervisor(
    request: Request,
    *,
    profile: Optional[str],
):
    normalized_profile = _normalize_optional_text(profile)
    if normalized_profile is None:
        supervisor = getattr(request.app.state, "hermes_supervisor", None)
        if supervisor is not None:
            return supervisor

    cache = getattr(request.app.state, "_hermes_supervisors_by_profile", None)
    if not isinstance(cache, dict):
        cache = {}
        request.app.state._hermes_supervisors_by_profile = cache

    cache_key = normalized_profile or ""
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    supervisor = build_hermes_supervisor_from_config(
        request.app.state.config,
        profile=normalized_profile,
    )
    if supervisor is not None:
        cache[cache_key] = supervisor
    return supervisor


__all__ = ["resolve_cached_hermes_supervisor"]
