"""Tests for centralized approval and sandbox policy mappings."""

import pytest

from codex_autorunner.agents.execution.policy import (
    ApprovalPolicy,
    PermissionPolicy,
    SandboxPolicy,
    build_codex_sandbox_policy,
    map_approval_to_permission,
    normalize_approval_policy,
    normalize_sandbox_policy,
    normalize_sandbox_policy_type,
)


class TestApprovalPolicyNormalization:
    """Test approval policy normalization."""

    def test_never_policy_aliases(self):
        """Test various aliases for 'never' policy."""
        assert normalize_approval_policy("never") == ApprovalPolicy.NEVER
        assert normalize_approval_policy("never ") == ApprovalPolicy.NEVER
        assert normalize_approval_policy("NEVER") == ApprovalPolicy.NEVER
        assert normalize_approval_policy("Never") == ApprovalPolicy.NEVER

    def test_on_failure_policy_aliases(self):
        """Test various aliases for 'on-failure' policy."""
        assert normalize_approval_policy("on-failure") == ApprovalPolicy.ON_FAILURE
        assert normalize_approval_policy("on_failure") == ApprovalPolicy.ON_FAILURE
        assert normalize_approval_policy("onfailure") == ApprovalPolicy.ON_FAILURE

    def test_on_request_policy_aliases(self):
        """Test various aliases for 'on-request' policy."""
        assert normalize_approval_policy("on-request") == ApprovalPolicy.ON_REQUEST
        assert normalize_approval_policy("on_request") == ApprovalPolicy.ON_REQUEST
        assert normalize_approval_policy("onrequest") == ApprovalPolicy.ON_REQUEST
        assert normalize_approval_policy("ask") == ApprovalPolicy.ON_REQUEST

    def test_untrusted_policy_aliases(self):
        """Test various aliases for 'untrusted' policy."""
        assert normalize_approval_policy("untrusted") == ApprovalPolicy.UNTRUSTED
        assert normalize_approval_policy("unless-trusted") == ApprovalPolicy.UNTRUSTED
        assert normalize_approval_policy("auto") == ApprovalPolicy.UNTRUSTED

    def test_none_returns_default(self):
        """Test that None returns 'never' as default."""
        assert normalize_approval_policy(None) == ApprovalPolicy.NEVER

    def test_invalid_policy_raises_error(self):
        """Test that invalid policy raises ValueError."""
        with pytest.raises(ValueError, match="Invalid approval policy"):
            normalize_approval_policy("invalid")
        with pytest.raises(ValueError, match="Invalid approval policy"):
            normalize_approval_policy("")


class TestSandboxPolicyNormalization:
    """Test sandbox policy normalization."""

    def test_danger_full_access_policy(self):
        """Test 'dangerFullAccess' policy normalization."""
        assert (
            normalize_sandbox_policy_type("dangerFullAccess")
            == SandboxPolicy.DANGER_FULL_ACCESS
        )
        assert (
            normalize_sandbox_policy_type("dangerfullaccess")
            == SandboxPolicy.DANGER_FULL_ACCESS
        )
        assert (
            normalize_sandbox_policy_type("DANGER_FULL_ACCESS")
            == SandboxPolicy.DANGER_FULL_ACCESS
        )

    def test_read_only_policy(self):
        """Test 'readOnly' policy normalization."""
        assert normalize_sandbox_policy_type("readOnly") == SandboxPolicy.READ_ONLY
        assert normalize_sandbox_policy_type("readonly") == SandboxPolicy.READ_ONLY
        assert normalize_sandbox_policy_type("READ_ONLY") == SandboxPolicy.READ_ONLY

    def test_workspace_write_policy(self):
        """Test 'workspaceWrite' policy normalization."""
        assert (
            normalize_sandbox_policy_type("workspaceWrite")
            == SandboxPolicy.WORKSPACE_WRITE
        )
        assert (
            normalize_sandbox_policy_type("workspacewrite")
            == SandboxPolicy.WORKSPACE_WRITE
        )

    def test_external_sandbox_policy(self):
        """Test 'externalSandbox' policy normalization."""
        assert (
            normalize_sandbox_policy_type("externalSandbox")
            == SandboxPolicy.EXTERNAL_SANDBOX
        )
        assert (
            normalize_sandbox_policy_type("externalsandbox")
            == SandboxPolicy.EXTERNAL_SANDBOX
        )

    def test_empty_string_returns_default(self):
        """Test that empty string returns default policy."""
        result = normalize_sandbox_policy_type("")
        assert result == SandboxPolicy.DANGER_FULL_ACCESS

    def test_none_returns_default(self):
        """Test that None returns default policy."""
        result = normalize_sandbox_policy(None)
        assert result == SandboxPolicy.DANGER_FULL_ACCESS

    def test_string_wraps_in_dict(self):
        """Test that string policy is wrapped in dict."""
        result = normalize_sandbox_policy("readOnly")
        assert isinstance(result, dict)
        assert result["type"] == SandboxPolicy.READ_ONLY

    def test_dict_normalizes_type_field(self):
        """Test that dict policy has normalized type field."""
        result = normalize_sandbox_policy({"type": "readonly"})
        assert isinstance(result, dict)
        assert result["type"] == SandboxPolicy.READ_ONLY

    def test_dict_preserves_other_fields(self):
        """Test that dict policy preserves other fields."""
        result = normalize_sandbox_policy(
            {"type": "workspaceWrite", "networkAccess": True, "writableRoots": ["/tmp"]}
        )
        assert isinstance(result, dict)
        assert result["type"] == SandboxPolicy.WORKSPACE_WRITE
        assert result["networkAccess"] is True
        assert result["writableRoots"] == ["/tmp"]

    def test_invalid_type_returns_as_is(self):
        """Test that invalid type returns default dict."""
        result = normalize_sandbox_policy_type("invalid-type")
        # Should return the original value if not in canonical map
        assert result == "invalid-type"


class TestBuildCodexSandboxPolicy:
    """Test building Codex sandbox policy from mode."""

    def test_danger_full_access_mode(self):
        """Test 'dangerFullAccess' mode returns string."""
        result = build_codex_sandbox_policy("dangerFullAccess")
        assert result == SandboxPolicy.DANGER_FULL_ACCESS

    def test_read_only_mode(self):
        """Test 'readOnly' mode returns string."""
        result = build_codex_sandbox_policy("readOnly")
        assert result == SandboxPolicy.READ_ONLY

    def test_workspace_write_mode_with_repo(self):
        """Test 'workspaceWrite' mode with repo_root returns dict."""
        result = build_codex_sandbox_policy(
            "workspaceWrite", repo_root="/path/to/repo", network_access=True
        )
        assert isinstance(result, dict)
        assert result["type"] == SandboxPolicy.WORKSPACE_WRITE
        assert result["writableRoots"] == ["/path/to/repo"]
        assert result["networkAccess"] is True

    def test_workspace_write_mode_without_repo(self):
        """Test 'workspaceWrite' mode without repo_root returns string."""
        result = build_codex_sandbox_policy("workspaceWrite")
        # When repo_root is None, should return normalized type
        assert result == SandboxPolicy.WORKSPACE_WRITE

    def test_none_mode_returns_default(self):
        """Test None mode returns default policy."""
        result = build_codex_sandbox_policy(None)
        assert result == SandboxPolicy.DANGER_FULL_ACCESS

    def test_workspace_write_network_access_false(self):
        """Test 'workspaceWrite' with network_access=False."""
        result = build_codex_sandbox_policy(
            "workspaceWrite", repo_root="/path/to/repo", network_access=False
        )
        assert isinstance(result, dict)
        assert result["networkAccess"] is False


class TestApprovalToPermissionMapping:
    """Test mapping approval policies to OpenCode permissions."""

    def test_never_maps_to_allow(self):
        """Test 'never' approval maps to 'allow' permission."""
        assert (
            map_approval_to_permission(ApprovalPolicy.NEVER) == PermissionPolicy.ALLOW
        )

    def test_on_failure_maps_to_ask(self):
        """Test 'on-failure' approval maps to 'ask' permission."""
        assert (
            map_approval_to_permission(ApprovalPolicy.ON_FAILURE)
            == PermissionPolicy.ASK
        )

    def test_on_request_maps_to_ask(self):
        """Test 'on-request' approval maps to 'ask' permission."""
        assert (
            map_approval_to_permission(ApprovalPolicy.ON_REQUEST)
            == PermissionPolicy.ASK
        )

    def test_untrusted_maps_to_ask(self):
        """Test 'untrusted' approval maps to 'ask' permission."""
        assert (
            map_approval_to_permission(ApprovalPolicy.UNTRUSTED) == PermissionPolicy.ASK
        )

    def test_none_returns_default_allow(self):
        """Test None returns default 'allow' permission."""
        assert (
            map_approval_to_permission(None, default=PermissionPolicy.ALLOW)
            == PermissionPolicy.ALLOW
        )

    def test_none_returns_custom_default(self):
        """Test None returns custom default permission."""
        assert (
            map_approval_to_permission(None, default=PermissionPolicy.DENY)
            == PermissionPolicy.DENY
        )

    def test_invalid_returns_default(self):
        """Test invalid policy returns default."""
        assert (
            map_approval_to_permission("invalid", default=PermissionPolicy.DENY)
            == PermissionPolicy.DENY
        )


class TestPolicyClasses:
    """Test policy class definitions."""

    def test_approval_policy_values(self):
        """Test ApprovalPolicy has all expected values."""
        assert ApprovalPolicy.NEVER in ApprovalPolicy.ALL_VALUES
        assert ApprovalPolicy.ON_FAILURE in ApprovalPolicy.ALL_VALUES
        assert ApprovalPolicy.ON_REQUEST in ApprovalPolicy.ALL_VALUES
        assert ApprovalPolicy.UNTRUSTED in ApprovalPolicy.ALL_VALUES

    def test_sandbox_policy_values(self):
        """Test SandboxPolicy has all expected values."""
        assert SandboxPolicy.DANGER_FULL_ACCESS in SandboxPolicy.ALL_VALUES
        assert SandboxPolicy.READ_ONLY in SandboxPolicy.ALL_VALUES
        assert SandboxPolicy.WORKSPACE_WRITE in SandboxPolicy.ALL_VALUES
        assert SandboxPolicy.EXTERNAL_SANDBOX in SandboxPolicy.ALL_VALUES

    def test_permission_policy_values(self):
        """Test PermissionPolicy has all expected values."""
        assert PermissionPolicy.ALLOW in PermissionPolicy.ALL_VALUES
        assert PermissionPolicy.DENY in PermissionPolicy.ALL_VALUES
        assert PermissionPolicy.ASK in PermissionPolicy.ALL_VALUES
