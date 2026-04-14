from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException, Request

from ....core.text_utils import _normalize_optional_text

logger = logging.getLogger(__name__)


def resolve_requested_agent_profile(
    request: Request,
    agent_id: str,
    requested_profile: Optional[str],
    *,
    default_profile: Optional[str] = None,
) -> Optional[str]:
    """Validate an optional agent profile against runtime-aware config state."""

    config = getattr(request.app.state, "config", None)
    profile_getter = getattr(config, "agent_profiles", None)
    default_profile_getter = getattr(config, "agent_default_profile", None)
    available_profiles: dict[str, Any] = {}
    if callable(profile_getter):
        try:
            available_profiles = profile_getter(agent_id) or {}
        except (ValueError, TypeError, AttributeError):
            available_profiles = {}

    resolved_profile = _normalize_optional_text(requested_profile)
    if resolved_profile is not None:
        if agent_id == "hermes":
            hermes_valid = set(available_profiles.keys())
            try:
                from ....integrations.chat.agents import chat_hermes_profile_options

                hermes_valid |= {
                    opt.profile
                    for opt in chat_hermes_profile_options(request.app.state)
                }
            except (
                ImportError,
                AttributeError,
                RuntimeError,
            ):  # intentional: optional integration path
                logger.debug("Failed to resolve hermes profile options", exc_info=True)
            if resolved_profile not in hermes_valid:
                raise HTTPException(status_code=400, detail="profile is invalid")
        elif resolved_profile not in available_profiles:
            raise HTTPException(status_code=400, detail="profile is invalid")
        return resolved_profile

    fallback_profiles: list[Optional[str]] = [
        _normalize_optional_text(default_profile),
    ]
    if callable(default_profile_getter):
        try:
            fallback_profiles.append(
                _normalize_optional_text(default_profile_getter(agent_id))
            )
        except (ValueError, TypeError, AttributeError):
            fallback_profiles.append(None)

    fallback_keys: set[str] = set(available_profiles.keys())
    if agent_id == "hermes":
        try:
            from ....integrations.chat.agents import chat_hermes_profile_options

            fallback_keys |= {
                opt.profile for opt in chat_hermes_profile_options(request.app.state)
            }
        except (
            ImportError,
            AttributeError,
            RuntimeError,
        ):  # intentional: optional integration path
            logger.debug(
                "Failed to resolve hermes fallback profile options", exc_info=True
            )

    for fallback_profile in fallback_profiles:
        if fallback_profile is not None:
            if agent_id == "hermes":
                if fallback_profile in fallback_keys:
                    return fallback_profile
            elif fallback_profile in available_profiles:
                return fallback_profile

    return None


__all__ = ["resolve_requested_agent_profile"]
