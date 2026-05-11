from __future__ import annotations

import pytest

from codex_autorunner.core.orchestration import (
    ChatSurface,
    ChatSurfaceDisplayMetadata,
    ChatSurfaceExternalConversationId,
    ChatSurfaceIdentity,
    ChatSurfaceResourceOwner,
    chat_surface_identity_dict,
    normalize_chat_surface_identity,
)


def test_identity_normalizes_kind_and_preserves_key() -> None:
    identity = ChatSurfaceIdentity(surface_kind=" Discord ", surface_key=" c:1 ")

    assert identity.surface_kind == "discord"
    assert identity.surface_key == "c:1"
    assert identity.to_dict() == {
        "surface_kind": "discord",
        "surface_key": "c:1",
    }
    assert identity.to_urn() == "discord:c%3A1"


def test_identity_parses_surface_urn() -> None:
    identity = ChatSurfaceIdentity.from_mapping({"surface_urn": "telegram:123%3Aroot"})

    assert identity == ChatSurfaceIdentity("telegram", "123:root")


@pytest.mark.parametrize(
    ("surface_kind", "surface_key", "match"),
    [
        ("", "key", "surface_kind is required"),
        ("tele gram", "key", "surface_kind must not contain whitespace"),
        ("telegram", "", "surface_key is required"),
        ("telegram", None, "surface_key is required"),
    ],
)
def test_identity_rejects_malformed_values(
    surface_kind: object,
    surface_key: object,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        ChatSurfaceIdentity(surface_kind=surface_kind, surface_key=surface_key)  # type: ignore[arg-type]


def test_identity_helper_returns_stable_pair() -> None:
    assert normalize_chat_surface_identity(
        surface_kind="Web",
        surface_key="pma.hermes",
    ) == ChatSurfaceIdentity("web", "pma.hermes")
    assert chat_surface_identity_dict(
        surface_kind="PMA",
        surface_key="managed-thread-1",
    ) == {"surface_kind": "pma", "surface_key": "managed-thread-1"}


def test_chat_surface_represents_discord_channel() -> None:
    surface = ChatSurface.from_mapping(
        {
            "surface_kind": "discord",
            "surface_key": "guild:channel:thread",
            "repo_id": "repo-1",
            "managed_thread_id": "thread-1",
            "display_name": "build channel",
            "external_conversation_id": "channel-123",
            "external_conversation_kind": "channel",
        }
    )

    assert surface.surface_kind == "discord"
    assert surface.surface_key == "guild:channel:thread"
    assert surface.owner == ChatSurfaceResourceOwner(
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        scope_urn="repo:repo-1",
    )
    assert surface.managed_thread_id == "thread-1"
    assert surface.external_conversation_ids == (
        ChatSurfaceExternalConversationId(
            provider="discord",
            conversation_id="channel-123",
            conversation_kind="channel",
        ),
    )
    serialized = surface.to_dict()
    assert serialized["surface_kind"] == "discord"
    assert serialized["surface_key"] == "guild:channel:thread"
    assert serialized["surface_urn"] == "discord:guild%3Achannel%3Athread"
    assert serialized["display_name"] == "build channel"


def test_chat_surface_represents_telegram_topic() -> None:
    surface = ChatSurface.from_mapping(
        {
            "surface_kind": "telegram",
            "surface_key": "-1001:77",
            "resource_kind": "repo",
            "resource_id": "repo-2",
            "display": {"title": "Topic 77"},
            "external_conversation_ids": [
                {
                    "provider": "telegram",
                    "conversation_id": "-1001",
                    "conversation_kind": "chat",
                },
                {
                    "provider": "telegram",
                    "conversation_id": "77",
                    "conversation_kind": "topic",
                },
            ],
        }
    )

    assert surface.owner.repo_id == "repo-2"
    assert surface.display == ChatSurfaceDisplayMetadata(title="Topic 77")
    assert [item.conversation_kind for item in surface.external_conversation_ids] == [
        "chat",
        "topic",
    ]


@pytest.mark.parametrize(
    ("surface_kind", "surface_key"),
    [
        ("web", "chat:repo-1:thread-1"),
        ("notification", "notification:notif-123"),
        ("pma", "pma.hermes.profile.m4-pma"),
    ],
)
def test_chat_surface_represents_web_notification_and_pma(
    surface_kind: str,
    surface_key: str,
) -> None:
    surface = ChatSurface.from_mapping(
        {
            "surface_kind": surface_kind,
            "surface_key": surface_key,
            "scope_urn": "hub",
            "status": "active",
        }
    )

    assert surface.surface_kind == surface_kind
    assert surface.surface_key == surface_key
    assert surface.owner.scope_urn == "hub"
    assert surface.lifecycle_status == "active"


def test_chat_surface_rejects_unknown_lifecycle() -> None:
    with pytest.raises(ValueError, match="unknown chat surface lifecycle status"):
        ChatSurface.from_mapping(
            {
                "surface_kind": "web",
                "surface_key": "chat-1",
                "lifecycle_status": "sleeping",
            }
        )
