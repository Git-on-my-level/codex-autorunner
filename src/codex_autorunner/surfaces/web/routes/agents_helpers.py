"""
Pure helpers for agent web route request parsing and response projection.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ....agents.types import ModelCatalog
from ..services.validation import normalize_agent_id


def normalize_path_agent_id(agent: str) -> str:
    # Path segments that decode to blank/whitespace are malformed and should
    # not silently fall back to the default agent.
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("Unknown agent")
    return normalize_agent_id(agent)


def parse_resume_after(
    since_event_id: Optional[int],
    last_event_id: Optional[str],
) -> Optional[int]:
    if since_event_id is not None:
        return since_event_id
    if not last_event_id:
        return None
    try:
        return int(last_event_id)
    except ValueError:
        return None


def serialize_model_catalog(catalog: ModelCatalog) -> dict[str, Any]:
    return {
        "default_model": catalog.default_model,
        "models": [
            {
                "id": model.id,
                "display_name": model.display_name,
                "supports_reasoning": model.supports_reasoning,
                "reasoning_options": list(model.reasoning_options),
            }
            for model in catalog.models
        ],
    }


def serialize_configured_profiles(
    configured_profiles: object,
) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    if not isinstance(configured_profiles, dict):
        return profiles
    for profile_id in sorted(configured_profiles):
        profile_cfg = configured_profiles.get(profile_id)
        display_name = None
        if profile_cfg is not None:
            display_name = getattr(profile_cfg, "display_name", None)
        profiles.append(
            {
                "id": profile_id,
                "display_name": (
                    str(display_name).strip()
                    if isinstance(display_name, str) and display_name.strip()
                    else profile_id
                ),
            }
        )
    return profiles


def merge_hermes_profile_options(
    profiles: list[dict[str, Any]],
    profile_options: Iterable[Any],
) -> list[dict[str, Any]]:
    existing_ids = {p["id"] for p in profiles}
    for option in profile_options:
        if option.profile in existing_ids:
            continue
        desc = option.description
        profiles.append(
            {
                "id": option.profile,
                "display_name": (
                    desc.strip()
                    if isinstance(desc, str) and desc.strip()
                    else option.profile
                ),
            }
        )
        existing_ids.add(option.profile)
    profiles.sort(key=lambda p: p["id"])
    return profiles


def serialize_agent_profiles(
    configured_profiles: object,
    default_profile: object,
    *,
    hermes_profile_options: Iterable[Any] = (),
) -> dict[str, Any]:
    profiles = serialize_configured_profiles(configured_profiles)
    if hermes_profile_options:
        profiles = merge_hermes_profile_options(profiles, hermes_profile_options)
    return {
        "default_profile": default_profile,
        "profiles": profiles,
    }


__all__ = [
    "merge_hermes_profile_options",
    "normalize_path_agent_id",
    "parse_resume_after",
    "serialize_agent_profiles",
    "serialize_configured_profiles",
    "serialize_model_catalog",
]
