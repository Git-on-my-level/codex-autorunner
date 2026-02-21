from __future__ import annotations


class DiscordError(Exception):
    """Base Discord integration error."""


class DiscordConfigError(DiscordError):
    """Discord integration configuration error."""


class DiscordAPIError(DiscordError):
    """Discord API request error."""
