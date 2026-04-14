"""Shared Hermes identity normalization helpers.

Provides one canonical path for decomposing Hermes agent identities
(across ticket flow, ticket chat, file chat, PMA, and bulk operations)
so that legacy alias-style agent IDs like ``hermes-m4-pma`` are
consistently resolved to ``agent="hermes", profile="m4-pma"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .registry import _normalize_optional_text, resolve_agent_runtime


@dataclass(frozen=True)
class CanonicalHermesIdentity:
    agent: str
    profile: Optional[str]


def _syntactic_alias_profile(
    agent_id: str,
    explicit_profile: Optional[str],
) -> Optional[CanonicalHermesIdentity]:
    normalized_agent = _normalize_optional_text(agent_id)
    if not normalized_agent or not is_hermes_alias_agent(normalized_agent):
        return None
    for separator in ("-", "_"):
        prefix = f"hermes{separator}"
        if normalized_agent.startswith(prefix):
            suffix = normalized_agent[len(prefix) :].strip()
            if not suffix:
                return None
            return CanonicalHermesIdentity(
                agent="hermes",
                profile=explicit_profile or suffix,
            )
    return None


def canonicalize_hermes_identity(
    agent_id: str,
    profile: Optional[str] = None,
    *,
    context: object = None,
) -> CanonicalHermesIdentity:
    """Normalize an agent ID (and optional profile) into canonical form.

    Legacy alias-style agent IDs (e.g. ``hermes-m4-pma``) are decomposed
    into ``agent="hermes", profile="m4-pma"``.  Plain agent IDs with an
    explicit profile are passed through after normalization.

    Returns a ``CanonicalHermesIdentity`` whose ``agent`` field is always
    the base agent name (e.g. ``"hermes"``) and whose ``profile`` field
    carries the resolved profile (may be ``None``).
    """
    normalized_agent = _normalize_optional_text(agent_id)
    if not normalized_agent:
        return CanonicalHermesIdentity(agent=agent_id, profile=profile)

    normalized_profile = _normalize_optional_text(profile)
    syntactic = _syntactic_alias_profile(agent_id, normalized_profile)

    try:
        resolution = resolve_agent_runtime(
            normalized_agent,
            normalized_profile,
            context=context,
        )
    except (ValueError, TypeError, RuntimeError):
        if syntactic is not None:
            return syntactic
        return CanonicalHermesIdentity(agent=agent_id, profile=profile)

    if resolution.logical_agent_id == "hermes" and normalized_agent != "hermes":
        return CanonicalHermesIdentity(
            agent="hermes",
            profile=resolution.logical_profile,
        )

    if resolution.logical_agent_id != normalized_agent:
        return CanonicalHermesIdentity(
            agent=resolution.logical_agent_id,
            profile=resolution.logical_profile,
        )

    if syntactic is not None:
        return syntactic

    return CanonicalHermesIdentity(
        agent=normalized_agent,
        profile=normalized_profile,
    )


def is_hermes_alias_agent(agent_id: str) -> bool:
    """Return whether *agent_id* looks like a Hermes alias (e.g. ``hermes-m4-pma``)."""
    normalized = (agent_id or "").strip().lower()
    if normalized == "hermes":
        return False
    return normalized.startswith("hermes-") or normalized.startswith("hermes_")
