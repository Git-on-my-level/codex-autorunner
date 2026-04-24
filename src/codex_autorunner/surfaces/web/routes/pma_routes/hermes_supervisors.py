from __future__ import annotations

from typing import Optional

from fastapi import Request

from .....core.text_utils import _normalize_optional_text
from ...services.pma import get_pma_request_context


def resolve_cached_hermes_supervisor(
    request: Request,
    *,
    profile: Optional[str],
):
    context = get_pma_request_context(request)
    normalized_profile = _normalize_optional_text(profile)
    if normalized_profile is None:
        supervisor = context.hermes_supervisor
        if supervisor is not None:
            return supervisor

    cache = context.hermes_supervisors_by_profile
    cache_key = normalized_profile or ""
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    supervisor = context.ports.build_hermes_supervisor(
        context.config,
        profile=normalized_profile,
    )
    if supervisor is not None:
        cache[cache_key] = supervisor
    return supervisor


__all__ = ["resolve_cached_hermes_supervisor"]
