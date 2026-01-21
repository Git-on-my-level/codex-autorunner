"""Centralized approval and sandbox policy mappings for Codex and OpenCode agents."""

from dataclasses import dataclass
from typing import Any, Optional, Union

# ========================================================================
# Approval Policies
# ========================================================================


class ApprovalPolicy:
    """Canonical approval policy values for both Codex and OpenCode."""

    NEVER = "never"
    ON_FAILURE = "on-failure"
    ON_REQUEST = "on-request"
    UNTRUSTED = "untrusted"

    ALL_VALUES = {NEVER, ON_FAILURE, ON_REQUEST, UNTRUSTED}


# ========================================================================
# Sandbox Policies (Codex)
# ========================================================================


class SandboxPolicy:
    """Canonical sandbox policy values for Codex app-server."""

    DANGER_FULL_ACCESS = "dangerFullAccess"
    READ_ONLY = "readOnly"
    WORKSPACE_WRITE = "workspaceWrite"
    EXTERNAL_SANDBOX = "externalSandbox"

    ALL_VALUES = {
        DANGER_FULL_ACCESS,
        READ_ONLY,
        WORKSPACE_WRITE,
        EXTERNAL_SANDBOX,
    }


# ========================================================================
# Permission Policies (OpenCode)
# ========================================================================


class PermissionPolicy:
    """Canonical permission policy values for OpenCode."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"

    ALL_VALUES = {ALLOW, DENY, ASK}


# ========================================================================
# Data Classes
# ========================================================================


@dataclass
class SandboxPolicyConfig:
    """Configuration for Codex sandbox policies."""

    policy: Union[str, dict[str, Any]]
    """Either a string policy type or full policy dict with type and options."""


@dataclass
class PolicyMapping:
    """Unified policy mapping for both Codex and OpenCode agents."""

    approval_policy: str
    sandbox_policy: Union[str, dict[str, Any]]
    permission_policy: Optional[str] = None


# ========================================================================
# Normalization Functions
# ========================================================================


def normalize_approval_policy(policy: Optional[str]) -> str:
    """Normalize approval policy to canonical value.

    Args:
        policy: Approval policy string (case-insensitive, various aliases accepted).

    Returns:
        Canonical approval policy value.

    Raises:
        ValueError: If policy is not a recognized value.
    """
    if policy is None:
        return ApprovalPolicy.NEVER

    if not isinstance(policy, str):
        raise ValueError(f"Invalid approval policy: {policy!r}")

    normalized = policy.strip()
    if not normalized:
        raise ValueError(f"Invalid approval policy: {policy!r}")

    normalized = normalized.lower()

    # Aliases for never
    if normalized in ("never", "no", "false", "0"):
        return ApprovalPolicy.NEVER

    # Aliases for on-failure
    if normalized in (
        "on-failure",
        "on_failure",
        "onfailure",
        "fail",
        "failure",
    ):
        return ApprovalPolicy.ON_FAILURE

    # Aliases for on-request
    if normalized in ("on-request", "on_request", "onrequest", "ask", "prompt"):
        return ApprovalPolicy.ON_REQUEST

    # Aliases for untrusted
    if normalized in (
        "untrusted",
        "unlesstrusted",
        "unless-trusted",
        "unless trusted",
        "auto",
    ):
        return ApprovalPolicy.UNTRUSTED

    raise ValueError(
        f"Invalid approval policy: {policy!r}. "
        f"Valid values: {', '.join(sorted(ApprovalPolicy.ALL_VALUES))}"
    )


def normalize_sandbox_policy(policy: Optional[Any]) -> Union[str, dict[str, Any]]:
    """Normalize sandbox policy to canonical value.

    Args:
        policy: Sandbox policy (string or dict with 'type' field).

    Returns:
        Normalized sandbox policy as string or dict.
    """
    if policy is None:
        return SandboxPolicy.DANGER_FULL_ACCESS

    # If it's a dict, normalize the type field
    if isinstance(policy, dict):
        policy_value = policy.copy()
        type_value = policy_value.get("type")
        if isinstance(type_value, str):
            policy_value["type"] = normalize_sandbox_policy_type(type_value)
        return policy_value

    # If it's a string, wrap in dict structure
    if isinstance(policy, str):
        normalized_type = normalize_sandbox_policy_type(policy)
        return {"type": normalized_type}

    # For other types, convert to string and wrap
    return {"type": SandboxPolicy.DANGER_FULL_ACCESS}


def normalize_sandbox_policy_type(raw: str) -> str:
    """Normalize sandbox policy type string to canonical value.

    Args:
        raw: Sandbox policy type string (case-insensitive).

    Returns:
        Canonical sandbox policy type.
    """
    if not raw:
        return SandboxPolicy.DANGER_FULL_ACCESS

    # Normalize case and remove special characters
    import re

    cleaned = re.sub(r"[^a-zA-Z0-9]+", "", raw.strip())
    if not cleaned:
        return SandboxPolicy.DANGER_FULL_ACCESS

    canonical = _SANDBOX_POLICY_CANONICAL.get(cleaned.lower())
    return canonical or raw.strip()


_SANDBOX_POLICY_CANONICAL = {
    "dangerfullaccess": SandboxPolicy.DANGER_FULL_ACCESS,
    "readonly": SandboxPolicy.READ_ONLY,
    "workspacewrite": SandboxPolicy.WORKSPACE_WRITE,
    "externalsandbox": SandboxPolicy.EXTERNAL_SANDBOX,
}


# ========================================================================
# Mapping Functions
# ========================================================================


def map_approval_to_permission(
    approval_policy: Optional[str], *, default: str = PermissionPolicy.ALLOW
) -> str:
    """Map approval policy to OpenCode permission policy.

    This maps Codex-style approval policies to OpenCode-style permission policies.

    Args:
        approval_policy: Codex approval policy.
        default: Default permission if policy is None or unrecognized.

    Returns:
        OpenCode permission policy (allow/deny/ask).
    """
    if approval_policy is None:
        return default

    try:
        normalized = normalize_approval_policy(approval_policy)
    except ValueError:
        # Invalid policy, return default
        return default

    # Direct matches
    if normalized == ApprovalPolicy.NEVER:
        return PermissionPolicy.ALLOW
    if normalized == ApprovalPolicy.ON_FAILURE:
        return PermissionPolicy.ASK
    if normalized == ApprovalPolicy.ON_REQUEST:
        return PermissionPolicy.ASK
    if normalized == ApprovalPolicy.UNTRUSTED:
        return PermissionPolicy.ASK

    return default


def build_codex_sandbox_policy(
    sandbox_mode: Optional[str],
    *,
    repo_root: Optional[Any] = None,
    network_access: bool = False,
) -> Union[str, dict[str, Any]]:
    """Build Codex sandbox policy from mode string.

    Args:
        sandbox_mode: Sandbox mode string.
        repo_root: Repository root path (for workspaceWrite policy).
        network_access: Whether to allow network access (for workspaceWrite).

    Returns:
        Sandbox policy string or dict.
    """
    if not sandbox_mode:
        return SandboxPolicy.DANGER_FULL_ACCESS

    normalized_mode = normalize_sandbox_policy_type(sandbox_mode)

    # workspaceWrite requires dict structure with writableRoots and networkAccess
    if normalized_mode == SandboxPolicy.WORKSPACE_WRITE and repo_root is not None:
        return {
            "type": SandboxPolicy.WORKSPACE_WRITE,
            "writableRoots": [str(repo_root)],
            "networkAccess": network_access,
        }

    # Other modes can be simple strings
    return normalized_mode


# ========================================================================
# Exports
# ========================================================================

__all__ = [
    "ApprovalPolicy",
    "SandboxPolicy",
    "PermissionPolicy",
    "SandboxPolicyConfig",
    "PolicyMapping",
    "normalize_approval_policy",
    "normalize_sandbox_policy",
    "normalize_sandbox_policy_type",
    "map_approval_to_permission",
    "build_codex_sandbox_policy",
]
