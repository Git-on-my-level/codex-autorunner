"""Shared approval-mode contract for chat surfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ApprovalModeContract:
    mode: str
    approval_policy: str
    sandbox_policy: str


APPROVAL_MODE_CONTRACTS = (
    ApprovalModeContract("yolo", "never", "dangerFullAccess"),
    ApprovalModeContract("safe", "on-request", "workspaceWrite"),
    ApprovalModeContract("read-only", "on-request", "readOnly"),
    ApprovalModeContract("auto", "on-request", "workspaceWrite"),
    ApprovalModeContract("full-access", "never", "dangerFullAccess"),
)
APPROVAL_MODE_VALUES = tuple(contract.mode for contract in APPROVAL_MODE_CONTRACTS)
APPROVAL_MODE_USAGE = "|".join(APPROVAL_MODE_VALUES)
APPROVAL_MODE_POLICIES = {
    contract.mode: (contract.approval_policy, contract.sandbox_policy)
    for contract in APPROVAL_MODE_CONTRACTS
}
_APPROVAL_MODE_ALIASES = {
    "readonly": "read-only",
    "read-only": "read-only",
    "read_only": "read-only",
    "full": "full-access",
    "fullaccess": "full-access",
    "full-access": "full-access",
    "full_access": "full-access",
    "agent": "auto",
    "auto": "auto",
    "safe": "safe",
    "yolo": "yolo",
}
_APPROVAL_MODE_COMMAND_ALIASES = {
    "off": "yolo",
    "disable": "yolo",
    "disabled": "yolo",
    "on": "safe",
    "enable": "safe",
    "enabled": "safe",
}


def normalize_approval_mode(
    raw: Optional[str],
    *,
    default: Optional[str] = None,
    include_command_aliases: bool = False,
) -> Optional[str]:
    if not isinstance(raw, str):
        return default
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    if not cleaned:
        return default
    aliases = dict(_APPROVAL_MODE_ALIASES)
    if include_command_aliases:
        aliases.update(_APPROVAL_MODE_COMMAND_ALIASES)
    normalized = aliases.get(cleaned, cleaned)
    if normalized in APPROVAL_MODE_POLICIES:
        return normalized
    return default


def resolve_approval_mode_policies(
    mode: Optional[str],
    *,
    default_approval_policy: Optional[str] = None,
    default_sandbox_policy: Optional[Any] = None,
    override_approval_policy: Optional[str] = None,
    override_sandbox_policy: Optional[Any] = None,
) -> tuple[Optional[str], Optional[Any]]:
    normalized = normalize_approval_mode(mode)
    approval_policy = default_approval_policy
    sandbox_policy = default_sandbox_policy
    if normalized is not None:
        approval_policy, sandbox_policy = APPROVAL_MODE_POLICIES[normalized]
    if override_approval_policy is not None:
        approval_policy = override_approval_policy
    if override_sandbox_policy is not None:
        sandbox_policy = override_sandbox_policy
    return approval_policy, sandbox_policy
