from __future__ import annotations

from typing import Optional

from .validation import normalize_string_lower


def select_terminal_subprotocol(protocol_header: Optional[str]) -> Optional[str]:
    if not protocol_header:
        return None
    for entry in protocol_header.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        if candidate == "car-token":
            return candidate
        if candidate.startswith("car-token-b64."):
            return candidate
        if candidate.startswith("car-token."):
            return candidate
    return None


def _normalize_profile_fragment(profile: Optional[str]) -> Optional[str]:
    normalized = normalize_string_lower(profile)
    return normalized or None


def session_key(repo: str, agent: str, profile: Optional[str] = None) -> str:
    normalized = normalize_string_lower(agent)
    normalized_profile = _normalize_profile_fragment(profile)
    if not normalized or normalized == "codex":
        return repo
    if normalized_profile:
        return f"{repo}:{normalized}@{normalized_profile}"
    return f"{repo}:{normalized}"


def _split_repo_agent(repo_key: str) -> tuple[str, str, Optional[str]]:
    if ":" not in repo_key:
        return repo_key, "codex", None
    repo, agent = repo_key.split(":", 1)
    profile = None
    if "@" in agent:
        agent, profile = agent.split("@", 1)
    return (
        repo,
        normalize_string_lower(agent) or "codex",
        _normalize_profile_fragment(profile),
    )


def remove_session_from_repo_mapping(
    repo_to_session: dict[str, str], *, session_id: str
) -> dict[str, str]:
    return {
        session_key(repo, agent, profile): sid
        for key, sid in repo_to_session.items()
        for repo, agent, profile in [_split_repo_agent(key)]
        if sid != session_id
    }
