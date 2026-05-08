from __future__ import annotations

from dataclasses import dataclass

from ..chat.collaboration_policy import (
    CollaborationEvaluationContext,
    build_discord_collaboration_policy,
    evaluate_collaboration_admission,
)


@dataclass(frozen=True)
class DiscordAllowlist:
    allowed_guild_ids: frozenset[str]
    allowed_channel_ids: frozenset[str]
    allowed_user_ids: frozenset[str]


def _as_id(value: object) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def _extract_user_id(payload: dict) -> str | None:
    member = payload.get("member")
    if isinstance(member, dict):
        member_user = member.get("user")
        if isinstance(member_user, dict):
            user_id = _as_id(member_user.get("id"))
            if user_id:
                return user_id
    user = payload.get("user")
    if isinstance(user, dict):
        return _as_id(user.get("id"))
    return None


def allowlist_allows(interaction_payload: dict, allowlist: DiscordAllowlist) -> bool:
    guild_id = _as_id(interaction_payload.get("guild_id"))
    channel_id = _as_id(interaction_payload.get("channel_id"))
    user_id = _extract_user_id(interaction_payload)
    policy = build_discord_collaboration_policy(
        allowed_guild_ids=allowlist.allowed_guild_ids,
        allowed_channel_ids=allowlist.allowed_channel_ids,
        allowed_user_ids=allowlist.allowed_user_ids,
    )
    result = evaluate_collaboration_admission(
        policy,
        CollaborationEvaluationContext(
            actor_id=user_id,
            container_id=guild_id,
            destination_id=channel_id,
        ),
    )
    return result.command_allowed
