from __future__ import annotations

from codex_autorunner.integrations.chat.collaboration_policy import (
    CollaborationDestinationPolicy,
    CollaborationEvaluationContext,
    CollaborationPolicy,
    build_discord_collaboration_policy,
    build_telegram_collaboration_policy,
    evaluate_collaboration_admission,
    evaluate_collaboration_policy,
)
from codex_autorunner.integrations.chat.turn_policy import PlainTextTurnContext


def test_discord_policy_denies_when_no_filters_or_destinations_are_configured() -> None:
    policy = build_discord_collaboration_policy(
        allowed_guild_ids=(),
        allowed_channel_ids=(),
        allowed_user_ids=(),
    )
    result = evaluate_collaboration_admission(
        policy,
        CollaborationEvaluationContext(
            actor_id="u1",
            container_id="g1",
            destination_id="c1",
        ),
    )
    assert result.outcome == "denied_destination"
    assert result.reason == "no_admission_filters_configured"


def test_telegram_policy_requires_both_actor_and_container_filters() -> None:
    policy = build_telegram_collaboration_policy(
        allowed_chat_ids=[-1001],
        allowed_user_ids=[],
        require_topics=False,
        trigger_mode="all",
    )
    result = evaluate_collaboration_admission(
        policy,
        CollaborationEvaluationContext(
            actor_id="42",
            container_id="-1001",
            destination_id="-1001",
        ),
    )
    assert result.outcome == "denied_actor"
    assert result.reason == "actor_filter_unconfigured"


def test_command_only_destination_allows_commands_but_not_plain_text() -> None:
    policy = CollaborationPolicy(
        platform="discord",
        allowed_actor_ids=frozenset({"u1"}),
        allowed_container_ids=frozenset({"g1"}),
        destinations=(
            CollaborationDestinationPolicy(
                container_id="g1",
                destination_id="c1",
                mode="command_only",
            ),
        ),
    )
    command_result = evaluate_collaboration_policy(
        policy,
        CollaborationEvaluationContext(
            actor_id="u1",
            container_id="g1",
            destination_id="c1",
            is_explicit_command=True,
        ),
    )
    message_result = evaluate_collaboration_policy(
        policy,
        CollaborationEvaluationContext(
            actor_id="u1",
            container_id="g1",
            destination_id="c1",
            plain_text=PlainTextTurnContext(text="ship it"),
        ),
    )
    assert command_result.command_allowed is True
    assert command_result.outcome == "command_only_destination"
    assert message_result.command_allowed is True
    assert message_result.should_start_turn is False
    assert message_result.outcome == "command_only_destination"


def test_silent_destination_stays_silent_for_plain_text_and_commands() -> None:
    policy = CollaborationPolicy(
        platform="telegram",
        allowed_actor_ids=frozenset({"42"}),
        allowed_container_ids=frozenset({"-1001"}),
        destinations=(
            CollaborationDestinationPolicy(
                container_id="-1001",
                destination_id="-1001",
                subdestination_id="7",
                mode="silent",
            ),
        ),
        required_configured_filters=frozenset({"actor", "container"}),
    )
    result = evaluate_collaboration_policy(
        policy,
        CollaborationEvaluationContext(
            actor_id="42",
            container_id="-1001",
            destination_id="-1001",
            subdestination_id="7",
            plain_text=PlainTextTurnContext(
                text="@bot hi",
                chat_type="supergroup",
                bot_username="bot",
            ),
        ),
    )
    assert result.outcome == "silent_destination"
    assert result.command_allowed is False
    assert result.should_start_turn is False


def test_mentions_trigger_ignores_plain_text_without_invocation() -> None:
    policy = CollaborationPolicy(
        platform="telegram",
        allowed_actor_ids=frozenset({"42"}),
        allowed_container_ids=frozenset({"-1001"}),
        required_configured_filters=frozenset({"actor", "container"}),
        default_plain_text_trigger="mentions",
    )
    result = evaluate_collaboration_policy(
        policy,
        CollaborationEvaluationContext(
            actor_id="42",
            container_id="-1001",
            destination_id="-1001",
            plain_text=PlainTextTurnContext(
                text="regular message",
                chat_type="supergroup",
                bot_username="bot",
            ),
        ),
    )
    assert result.outcome == "plain_text_ignored"
    assert result.reason == "plain_text_trigger_mentions"


def test_telegram_policy_requires_subdestination_when_topics_are_required() -> None:
    policy = build_telegram_collaboration_policy(
        allowed_chat_ids=[-1001],
        allowed_user_ids=[42],
        require_topics=True,
        trigger_mode="all",
    )
    result = evaluate_collaboration_policy(
        policy,
        CollaborationEvaluationContext(
            actor_id="42",
            container_id="-1001",
            destination_id="-1001",
            plain_text=PlainTextTurnContext(
                text="hello",
                chat_type="supergroup",
            ),
        ),
    )
    assert result.outcome == "denied_destination"
    assert result.reason == "subdestination_required"
    assert result.command_allowed is False


def test_builder_accepts_trigger_alias_and_destination_rules() -> None:
    policy = build_telegram_collaboration_policy(
        allowed_chat_ids=[-1001],
        allowed_user_ids=[42],
        require_topics=False,
        trigger_mode="all",
        collaboration_raw={
            "default_mode": "active",
            "destinations": [
                {
                    "chat_id": -1001,
                    "thread_id": 5,
                    "mode": "command_only",
                    "trigger_mode": "all",
                }
            ],
        },
    )
    assert policy.default_plain_text_trigger == "always"
    assert policy.destinations[0].plain_text_trigger == "always"
    assert policy.destinations[0].mode == "command_only"
