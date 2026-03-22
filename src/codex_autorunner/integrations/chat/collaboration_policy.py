from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Mapping, Optional

from .turn_policy import PlainTextTurnContext, should_trigger_plain_text_turn

DestinationMode = Literal["active", "command_only", "silent", "denied"]
PlainTextTriggerMode = Literal["always", "mentions", "disabled"]
RequiredFilterDimension = Literal["actor", "container"]
CollaborationOutcome = Literal[
    "command_allowed",
    "command_only_destination",
    "denied_actor",
    "denied_container",
    "denied_destination",
    "plain_text_ignored",
    "silent_destination",
    "turn_started",
]

DEFAULT_DESTINATION_MODE: DestinationMode = "active"
DEFAULT_PLAIN_TEXT_TRIGGER: PlainTextTriggerMode = "always"
TELEGRAM_DEFAULT_DESTINATION_MODE: DestinationMode = "command_only"
DESTINATION_MODE_OPTIONS = {"active", "command_only", "silent", "denied"}
PLAIN_TEXT_TRIGGER_OPTIONS = {"always", "mentions", "disabled"}
_TRIGGER_ALIASES = {
    "all": "always",
    "always": "always",
    "mentions": "mentions",
    "disabled": "disabled",
}


class CollaborationPolicyError(ValueError):
    """Raised when shared collaboration policy config is invalid."""


@dataclass(frozen=True)
class CollaborationDestinationPolicy:
    destination_id: str
    container_id: Optional[str] = None
    subdestination_id: Optional[str] = None
    mode: DestinationMode = DEFAULT_DESTINATION_MODE
    plain_text_trigger: PlainTextTriggerMode = DEFAULT_PLAIN_TEXT_TRIGGER
    name: Optional[str] = None

    def matches(
        self,
        *,
        container_id: Optional[str],
        destination_id: Optional[str],
        subdestination_id: Optional[str],
    ) -> bool:
        if destination_id != self.destination_id:
            return False
        if self.container_id is not None and container_id != self.container_id:
            return False
        if (
            self.subdestination_id is not None
            and subdestination_id != self.subdestination_id
        ):
            return False
        return True

    def specificity(self) -> tuple[int, int]:
        return (
            1 if self.subdestination_id is not None else 0,
            1 if self.container_id is not None else 0,
        )


@dataclass(frozen=True)
class CollaborationPolicy:
    platform: str
    allowed_actor_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_container_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_destination_ids: frozenset[str] = field(default_factory=frozenset)
    destinations: tuple[CollaborationDestinationPolicy, ...] = ()
    required_configured_filters: frozenset[RequiredFilterDimension] = field(
        default_factory=frozenset
    )
    deny_if_all_admission_filters_empty: bool = False
    require_subdestination: bool = False
    default_mode: DestinationMode = DEFAULT_DESTINATION_MODE
    default_plain_text_trigger: PlainTextTriggerMode = DEFAULT_PLAIN_TEXT_TRIGGER

    def has_any_admission_filters(self) -> bool:
        return bool(
            self.allowed_actor_ids
            or self.allowed_container_ids
            or self.allowed_destination_ids
            or self.destinations
        )


@dataclass(frozen=True)
class CollaborationEvaluationContext:
    actor_id: Optional[str]
    container_id: Optional[str]
    destination_id: Optional[str]
    subdestination_id: Optional[str] = None
    is_explicit_command: bool = False
    plain_text: Optional[PlainTextTurnContext] = None


@dataclass(frozen=True)
class CollaborationEvaluationResult:
    outcome: CollaborationOutcome
    allowed: bool
    command_allowed: bool
    should_start_turn: bool
    actor_allowed: bool
    container_allowed: bool
    destination_allowed: bool
    destination_mode: DestinationMode
    plain_text_trigger: PlainTextTriggerMode
    reason: str
    matched_destination: Optional[CollaborationDestinationPolicy] = None

    def log_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "policy_outcome": self.outcome,
            "policy_reason": self.reason,
            "policy_destination_mode": self.destination_mode,
            "policy_plain_text_trigger": self.plain_text_trigger,
            "policy_command_allowed": self.command_allowed,
            "policy_should_start_turn": self.should_start_turn,
            "policy_actor_allowed": self.actor_allowed,
            "policy_container_allowed": self.container_allowed,
            "policy_destination_allowed": self.destination_allowed,
        }
        if self.matched_destination is not None:
            fields["policy_matched_destination_id"] = (
                self.matched_destination.destination_id
            )
            if self.matched_destination.container_id is not None:
                fields["policy_matched_container_id"] = (
                    self.matched_destination.container_id
                )
            if self.matched_destination.subdestination_id is not None:
                fields["policy_matched_subdestination_id"] = (
                    self.matched_destination.subdestination_id
                )
            if self.matched_destination.name:
                fields["policy_matched_name"] = self.matched_destination.name
        return fields


def evaluate_collaboration_policy(
    policy: CollaborationPolicy,
    context: CollaborationEvaluationContext,
    *,
    plain_text_turn_fn: Callable[..., bool] = should_trigger_plain_text_turn,
) -> CollaborationEvaluationResult:
    denied = _evaluate_admission(policy, context)
    if denied is not None:
        return denied

    matched = _match_destination(policy, context)
    destination_mode = matched.mode if matched is not None else policy.default_mode
    plain_text_trigger = (
        matched.plain_text_trigger
        if matched is not None
        else policy.default_plain_text_trigger
    )

    if destination_mode == "denied":
        return _deny(
            outcome="denied_destination",
            reason="destination_mode_denied",
            mode=destination_mode,
            actor_allowed=True,
            container_allowed=True,
            destination_allowed=False,
            trigger=plain_text_trigger,
            matched_destination=matched,
        )

    if destination_mode == "silent":
        return CollaborationEvaluationResult(
            outcome="silent_destination",
            allowed=False,
            command_allowed=False,
            should_start_turn=False,
            actor_allowed=True,
            container_allowed=True,
            destination_allowed=True,
            destination_mode=destination_mode,
            plain_text_trigger="disabled",
            reason="silent_destination",
            matched_destination=matched,
        )

    if context.is_explicit_command:
        return CollaborationEvaluationResult(
            outcome=(
                "command_only_destination"
                if destination_mode == "command_only"
                else "command_allowed"
            ),
            allowed=True,
            command_allowed=True,
            should_start_turn=False,
            actor_allowed=True,
            container_allowed=True,
            destination_allowed=True,
            destination_mode=destination_mode,
            plain_text_trigger=(
                "disabled" if destination_mode == "command_only" else plain_text_trigger
            ),
            reason=(
                "explicit_command_allowed"
                if destination_mode == "active"
                else "explicit_command_allowed_command_only"
            ),
            matched_destination=matched,
        )

    if destination_mode == "command_only" or plain_text_trigger == "disabled":
        return CollaborationEvaluationResult(
            outcome="command_only_destination",
            allowed=False,
            command_allowed=True,
            should_start_turn=False,
            actor_allowed=True,
            container_allowed=True,
            destination_allowed=True,
            destination_mode="command_only",
            plain_text_trigger="disabled",
            reason="plain_text_disabled",
            matched_destination=matched,
        )

    should_start_turn = _should_start_plain_text_turn(
        plain_text_trigger,
        context.plain_text,
        plain_text_turn_fn=plain_text_turn_fn,
    )
    if not should_start_turn:
        return CollaborationEvaluationResult(
            outcome="plain_text_ignored",
            allowed=False,
            command_allowed=True,
            should_start_turn=False,
            actor_allowed=True,
            container_allowed=True,
            destination_allowed=True,
            destination_mode=destination_mode,
            plain_text_trigger=plain_text_trigger,
            reason=f"plain_text_trigger_{plain_text_trigger}",
            matched_destination=matched,
        )

    return CollaborationEvaluationResult(
        outcome="turn_started",
        allowed=True,
        command_allowed=True,
        should_start_turn=True,
        actor_allowed=True,
        container_allowed=True,
        destination_allowed=True,
        destination_mode=destination_mode,
        plain_text_trigger=plain_text_trigger,
        reason="plain_text_trigger_matched",
        matched_destination=matched,
    )


def evaluate_collaboration_admission(
    policy: CollaborationPolicy,
    context: CollaborationEvaluationContext,
) -> CollaborationEvaluationResult:
    denied = _evaluate_admission(policy, context)
    if denied is not None:
        return denied
    matched = _match_destination(policy, context)
    destination_mode = matched.mode if matched is not None else policy.default_mode
    plain_text_trigger = (
        matched.plain_text_trigger
        if matched is not None
        else policy.default_plain_text_trigger
    )
    return CollaborationEvaluationResult(
        outcome="command_allowed",
        allowed=True,
        command_allowed=True,
        should_start_turn=False,
        actor_allowed=True,
        container_allowed=True,
        destination_allowed=True,
        destination_mode=destination_mode,
        plain_text_trigger=plain_text_trigger,
        reason="admission_allowed",
        matched_destination=matched,
    )


def build_telegram_collaboration_policy(
    *,
    allowed_chat_ids: Iterable[int],
    allowed_user_ids: Iterable[int],
    require_topics: bool,
    trigger_mode: str,
    collaboration_raw: Optional[Mapping[str, Any]] = None,
    shared_raw: Optional[Mapping[str, Any]] = None,
) -> CollaborationPolicy:
    shared_actors = _mapping(shared_raw).get("actors")
    actor_fallback = _mapping(shared_actors).get("allowed_user_ids")
    surface_cfg = _mapping(collaboration_raw)
    actor_source = surface_cfg.get("allowed_user_ids", actor_fallback)
    container_source = surface_cfg.get("allowed_chat_ids", list(allowed_chat_ids))
    require_subdestination = _bool_or_default(
        surface_cfg.get("require_topics"),
        default=require_topics,
        key="collaboration_policy.telegram.require_topics",
    )

    return CollaborationPolicy(
        platform="telegram",
        allowed_actor_ids=_parse_string_set(
            actor_source if actor_source is not None else list(allowed_user_ids)
        ),
        allowed_container_ids=_parse_string_set(container_source),
        destinations=_parse_telegram_destinations(surface_cfg.get("destinations")),
        required_configured_filters=frozenset({"actor", "container"}),
        require_subdestination=require_subdestination,
        default_mode=_parse_destination_mode(
            surface_cfg.get("default_mode", TELEGRAM_DEFAULT_DESTINATION_MODE),
            key="collaboration_policy.telegram.default_mode",
        ),
        default_plain_text_trigger=_parse_plain_text_trigger(
            surface_cfg.get("default_plain_text_trigger", trigger_mode),
            key="collaboration_policy.telegram.default_plain_text_trigger",
        ),
    )


def build_discord_collaboration_policy(
    *,
    allowed_guild_ids: Iterable[str],
    allowed_channel_ids: Iterable[str],
    allowed_user_ids: Iterable[str],
    collaboration_raw: Optional[Mapping[str, Any]] = None,
    shared_raw: Optional[Mapping[str, Any]] = None,
) -> CollaborationPolicy:
    shared_actors = _mapping(shared_raw).get("actors")
    actor_fallback = _mapping(shared_actors).get("allowed_user_ids")
    surface_cfg = _mapping(collaboration_raw)
    actor_source = surface_cfg.get("allowed_user_ids", actor_fallback)
    return CollaborationPolicy(
        platform="discord",
        allowed_actor_ids=_parse_string_set(
            actor_source if actor_source is not None else list(allowed_user_ids)
        ),
        allowed_container_ids=_parse_string_set(
            surface_cfg.get("allowed_guild_ids", list(allowed_guild_ids))
        ),
        allowed_destination_ids=_parse_string_set(
            surface_cfg.get("allowed_channel_ids", list(allowed_channel_ids))
        ),
        destinations=_parse_discord_destinations(surface_cfg.get("destinations")),
        deny_if_all_admission_filters_empty=True,
        default_mode=_parse_destination_mode(
            surface_cfg.get("default_mode", DEFAULT_DESTINATION_MODE),
            key="collaboration_policy.discord.default_mode",
        ),
        default_plain_text_trigger=_parse_plain_text_trigger(
            surface_cfg.get(
                "default_plain_text_trigger",
                DEFAULT_PLAIN_TEXT_TRIGGER,
            ),
            key="collaboration_policy.discord.default_plain_text_trigger",
        ),
    )


def _mapping(raw: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def _parse_string_set(raw: Any) -> frozenset[str]:
    if raw is None:
        return frozenset()
    items = raw if isinstance(raw, (list, tuple, set, frozenset)) else [raw]
    values: list[str] = []
    for item in items:
        token = str(item).strip()
        if token:
            values.append(token)
    return frozenset(values)


def _parse_plain_text_trigger(raw: Any, *, key: str) -> PlainTextTriggerMode:
    token = str(raw or DEFAULT_PLAIN_TEXT_TRIGGER).strip().lower()
    normalized = _TRIGGER_ALIASES.get(token)
    if normalized is None or normalized not in PLAIN_TEXT_TRIGGER_OPTIONS:
        raise CollaborationPolicyError(
            f"{key} must be one of {sorted(PLAIN_TEXT_TRIGGER_OPTIONS | {'all'})}"
        )
    return normalized  # type: ignore[return-value]


def _parse_destination_mode(raw: Any, *, key: str) -> DestinationMode:
    token = str(raw or DEFAULT_DESTINATION_MODE).strip().lower()
    if token not in DESTINATION_MODE_OPTIONS:
        raise CollaborationPolicyError(
            f"{key} must be one of {sorted(DESTINATION_MODE_OPTIONS)}"
        )
    return token  # type: ignore[return-value]


def _bool_or_default(raw: Any, *, default: bool, key: str) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    raise CollaborationPolicyError(f"{key} must be boolean")


def _parse_telegram_destinations(
    raw: Any,
) -> tuple[CollaborationDestinationPolicy, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CollaborationPolicyError(
            "collaboration_policy.telegram.destinations must be a list"
        )
    parsed: list[CollaborationDestinationPolicy] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CollaborationPolicyError(
                "collaboration_policy.telegram.destinations entries must be mappings"
            )
        chat_id = _require_string_id(
            item.get("chat_id"),
            key=f"collaboration_policy.telegram.destinations[{index}].chat_id",
        )
        thread_id = _optional_string_id(item.get("thread_id"))
        parsed.append(
            CollaborationDestinationPolicy(
                container_id=chat_id,
                destination_id=chat_id,
                subdestination_id=thread_id,
                mode=_parse_destination_mode(
                    item.get("mode", DEFAULT_DESTINATION_MODE),
                    key=f"collaboration_policy.telegram.destinations[{index}].mode",
                ),
                plain_text_trigger=_parse_plain_text_trigger(
                    item.get(
                        "plain_text_trigger",
                        item.get("trigger_mode", DEFAULT_PLAIN_TEXT_TRIGGER),
                    ),
                    key=(
                        "collaboration_policy.telegram.destinations"
                        f"[{index}].plain_text_trigger"
                    ),
                ),
                name=_optional_string(item.get("name")),
            )
        )
    return tuple(parsed)


def _parse_discord_destinations(
    raw: Any,
) -> tuple[CollaborationDestinationPolicy, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CollaborationPolicyError(
            "collaboration_policy.discord.destinations must be a list"
        )
    parsed: list[CollaborationDestinationPolicy] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CollaborationPolicyError(
                "collaboration_policy.discord.destinations entries must be mappings"
            )
        channel_id = _require_string_id(
            item.get("channel_id"),
            key=f"collaboration_policy.discord.destinations[{index}].channel_id",
        )
        parsed.append(
            CollaborationDestinationPolicy(
                container_id=_optional_string_id(item.get("guild_id")),
                destination_id=channel_id,
                mode=_parse_destination_mode(
                    item.get("mode", DEFAULT_DESTINATION_MODE),
                    key=f"collaboration_policy.discord.destinations[{index}].mode",
                ),
                plain_text_trigger=_parse_plain_text_trigger(
                    item.get(
                        "plain_text_trigger",
                        item.get("trigger_mode", DEFAULT_PLAIN_TEXT_TRIGGER),
                    ),
                    key=(
                        "collaboration_policy.discord.destinations"
                        f"[{index}].plain_text_trigger"
                    ),
                ),
                name=_optional_string(item.get("name")),
            )
        )
    return tuple(parsed)


def _optional_string_id(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    token = str(raw).strip()
    return token or None


def _require_string_id(raw: Any, *, key: str) -> str:
    token = _optional_string_id(raw)
    if token is None:
        raise CollaborationPolicyError(f"{key} must be a non-empty string/int ID")
    return token


def _optional_string(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    token = str(raw).strip()
    return token or None


def _match_destination(
    policy: CollaborationPolicy,
    context: CollaborationEvaluationContext,
) -> Optional[CollaborationDestinationPolicy]:
    matched = [
        destination
        for destination in policy.destinations
        if destination.matches(
            container_id=context.container_id,
            destination_id=context.destination_id,
            subdestination_id=context.subdestination_id,
        )
    ]
    if not matched:
        return None
    matched.sort(key=lambda destination: destination.specificity(), reverse=True)
    return matched[0]


def _evaluate_admission(
    policy: CollaborationPolicy,
    context: CollaborationEvaluationContext,
) -> Optional[CollaborationEvaluationResult]:
    if "actor" in policy.required_configured_filters and not policy.allowed_actor_ids:
        return _deny(
            outcome="denied_actor",
            reason="actor_filter_unconfigured",
            mode="denied",
        )

    if (
        "container" in policy.required_configured_filters
        and not policy.allowed_container_ids
    ):
        return _deny(
            outcome="denied_container",
            reason="container_filter_unconfigured",
            mode="denied",
        )

    if (
        policy.deny_if_all_admission_filters_empty
        and not policy.has_any_admission_filters()
    ):
        return _deny(
            outcome="denied_destination",
            reason="no_admission_filters_configured",
            mode="denied",
        )

    if policy.allowed_actor_ids:
        if context.actor_id is None or context.actor_id not in policy.allowed_actor_ids:
            return _deny(
                outcome="denied_actor",
                reason="actor_not_allowed",
                mode="denied",
                actor_allowed=False,
            )

    if policy.allowed_container_ids:
        if (
            context.container_id is None
            or context.container_id not in policy.allowed_container_ids
        ):
            return _deny(
                outcome="denied_container",
                reason="container_not_allowed",
                mode="denied",
                actor_allowed=True,
                container_allowed=False,
            )

    if policy.allowed_destination_ids:
        if (
            context.destination_id is None
            or context.destination_id not in policy.allowed_destination_ids
        ):
            return _deny(
                outcome="denied_destination",
                reason="destination_not_allowed",
                mode="denied",
                actor_allowed=True,
                container_allowed=True,
                destination_allowed=False,
            )

    if policy.require_subdestination and context.subdestination_id is None:
        return _deny(
            outcome="denied_destination",
            reason="subdestination_required",
            mode="denied",
            actor_allowed=True,
            container_allowed=True,
            destination_allowed=False,
        )

    return None


def _should_start_plain_text_turn(
    trigger: PlainTextTriggerMode,
    context: Optional[PlainTextTurnContext],
    *,
    plain_text_turn_fn: Callable[..., bool],
) -> bool:
    if trigger == "disabled":
        return False
    if context is None:
        return trigger == "always"
    return plain_text_turn_fn(mode=trigger, context=context)


def _deny(
    *,
    outcome: CollaborationOutcome,
    reason: str,
    mode: DestinationMode,
    actor_allowed: bool = False,
    container_allowed: bool = False,
    destination_allowed: bool = False,
    trigger: PlainTextTriggerMode = "disabled",
    matched_destination: Optional[CollaborationDestinationPolicy] = None,
) -> CollaborationEvaluationResult:
    return CollaborationEvaluationResult(
        outcome=outcome,
        allowed=False,
        command_allowed=False,
        should_start_turn=False,
        actor_allowed=actor_allowed,
        container_allowed=container_allowed,
        destination_allowed=destination_allowed,
        destination_mode=mode,
        plain_text_trigger=trigger,
        reason=reason,
        matched_destination=matched_destination,
    )
