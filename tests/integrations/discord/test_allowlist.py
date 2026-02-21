from __future__ import annotations

from codex_autorunner.integrations.discord.allowlist import (
    DiscordAllowlist,
    allowlist_allows,
)


def _payload(
    *, guild_id: str = "g1", channel_id: str = "c1", user_id: str = "u1"
) -> dict:
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "member": {"user": {"id": user_id}},
    }


def test_allowlist_denies_when_no_allowlist_set_is_configured() -> None:
    payload = _payload()
    allowlist = DiscordAllowlist(
        allowed_guild_ids=frozenset(),
        allowed_channel_ids=frozenset(),
        allowed_user_ids=frozenset(),
    )
    assert allowlist_allows(payload, allowlist) is False


def test_allowlist_requires_membership_in_all_sets() -> None:
    allowlist = DiscordAllowlist(
        allowed_guild_ids=frozenset({"g1"}),
        allowed_channel_ids=frozenset({"c1"}),
        allowed_user_ids=frozenset({"u1"}),
    )
    assert allowlist_allows(_payload(), allowlist) is True
    assert allowlist_allows(_payload(guild_id="other"), allowlist) is False
    assert allowlist_allows(_payload(channel_id="other"), allowlist) is False
    assert allowlist_allows(_payload(user_id="other"), allowlist) is False


def test_allowlist_enforces_only_configured_dimensions() -> None:
    guild_only = DiscordAllowlist(
        allowed_guild_ids=frozenset({"g1"}),
        allowed_channel_ids=frozenset(),
        allowed_user_ids=frozenset(),
    )
    assert allowlist_allows(_payload(), guild_only) is True
    assert allowlist_allows(_payload(guild_id="other"), guild_only) is False

    user_only = DiscordAllowlist(
        allowed_guild_ids=frozenset(),
        allowed_channel_ids=frozenset(),
        allowed_user_ids=frozenset({"u1"}),
    )
    assert allowlist_allows(_payload(), user_only) is True
    assert allowlist_allows(_payload(user_id="other"), user_only) is False


def test_allowlist_reads_dm_user_shape() -> None:
    allowlist = DiscordAllowlist(
        allowed_guild_ids=frozenset({"g1"}),
        allowed_channel_ids=frozenset({"c1"}),
        allowed_user_ids=frozenset({"u1"}),
    )
    payload = {
        "guild_id": "g1",
        "channel_id": "c1",
        "user": {"id": "u1"},
    }
    assert allowlist_allows(payload, allowlist) is True
