from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, cast

from .text_utils import _mapping, _normalize_optional_text

MutationPolicyValue = Literal["allow", "deny", "require_approval"]

MUTATION_POLICY_ALLOWED_VALUES: tuple[MutationPolicyValue, ...] = (
    "allow",
    "deny",
    "require_approval",
)
MUTATION_POLICY_ACTION_TYPES: tuple[str, ...] = (
    "enqueue_managed_turn",
    "notify_chat",
    "react_pr_review_comment",
    "post_pr_comment",
    "add_labels",
    "merge_pr",
)
_MUTATION_POLICY_DEFAULTS: dict[str, MutationPolicyValue] = {
    "enqueue_managed_turn": "allow",
    "notify_chat": "allow",
    "react_pr_review_comment": "deny",
    "post_pr_comment": "deny",
    "add_labels": "deny",
    "merge_pr": "deny",
}
_MUTATION_POLICY_VALUE_ALIASES: dict[str, MutationPolicyValue] = {
    "allow": "allow",
    "allowed": "allow",
    "deny": "deny",
    "denied": "deny",
    "require_approval": "require_approval",
    "require-approval": "require_approval",
    "review": "require_approval",
    "ask": "require_approval",
}


def _looks_like_policy_mapping(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in MUTATION_POLICY_ACTION_TYPES)


def normalize_mutation_policy_value(value: object) -> Optional[MutationPolicyValue]:
    if isinstance(value, bool):
        return "allow" if value else "deny"
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    return _MUTATION_POLICY_VALUE_ALIASES.get(normalized.lower())


def resolve_mutation_policy_mapping(config: object) -> Mapping[str, Any]:
    mapping = _mapping(config)
    policy = _mapping(mapping.get("policy"))
    if policy:
        return policy
    automation = _mapping(mapping.get("automation"))
    if automation:
        nested = resolve_mutation_policy_mapping(automation)
        if nested:
            return nested
    github = _mapping(mapping.get("github"))
    if github:
        nested = resolve_mutation_policy_mapping(github)
        if nested:
            return nested
    if _looks_like_policy_mapping(mapping):
        return mapping
    return {}


def default_mutation_policy_for_action(action_type: str) -> MutationPolicyValue:
    normalized_action = _normalize_optional_text(action_type)
    if normalized_action is None:
        return "deny"
    return _MUTATION_POLICY_DEFAULTS.get(normalized_action, "deny")


@dataclass(frozen=True)
class PolicyDecision:
    action_type: str
    decision: MutationPolicyValue
    provider: Optional[str]
    repo_id: Optional[str]
    binding_id: Optional[str]
    reason: str
    source: Literal["config", "default"]

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    @property
    def denied(self) -> bool:
        return self.decision == "deny"

    @property
    def requires_approval(self) -> bool:
        return self.decision == "require_approval"


def evaluate(
    action_type: str,
    *,
    provider: Optional[str] = None,
    repo_id: Optional[str] = None,
    binding_id: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    config: object = None,
) -> PolicyDecision:
    normalized_action = _normalize_optional_text(action_type) or ""
    policy_mapping = resolve_mutation_policy_mapping(config)
    raw_policy_value = policy_mapping.get(normalized_action)
    resolved_policy = normalize_mutation_policy_value(raw_policy_value)
    _ = payload
    if resolved_policy is not None:
        return PolicyDecision(
            action_type=normalized_action,
            decision=resolved_policy,
            provider=_normalize_optional_text(provider),
            repo_id=_normalize_optional_text(repo_id),
            binding_id=_normalize_optional_text(binding_id),
            reason=f"Configured policy for '{normalized_action}'",
            source="config",
        )

    default_policy = default_mutation_policy_for_action(normalized_action)
    if normalized_action in _MUTATION_POLICY_DEFAULTS:
        reason = f"Default policy for '{normalized_action}'"
    else:
        reason = f"Unknown mutation action '{normalized_action}' defaults to deny"
    return PolicyDecision(
        action_type=normalized_action,
        decision=cast(MutationPolicyValue, default_policy),
        provider=_normalize_optional_text(provider),
        repo_id=_normalize_optional_text(repo_id),
        binding_id=_normalize_optional_text(binding_id),
        reason=reason,
        source="default",
    )


__all__ = [
    "MUTATION_POLICY_ACTION_TYPES",
    "MUTATION_POLICY_ALLOWED_VALUES",
    "MutationPolicyValue",
    "PolicyDecision",
    "default_mutation_policy_for_action",
    "evaluate",
    "normalize_mutation_policy_value",
    "resolve_mutation_policy_mapping",
]
