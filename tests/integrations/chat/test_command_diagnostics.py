from pathlib import Path

from codex_autorunner.integrations.chat.command_diagnostics import (
    ActiveFlowInfo,
    build_debug_text,
    build_ids_text,
    build_status_text,
)


class TestBuildStatusText:
    def test_unbound_channel_returns_not_bound_message(self):
        binding = None
        collaboration_summary_lines = None
        active_flow = None
        channel_id = "channel-123"

        lines = build_status_text(
            binding, collaboration_summary_lines, active_flow, channel_id
        )

        assert "This channel is not bound." in lines[0]
        assert "Use /car bind workspace:<workspace>" in lines[1]

    def test_bound_workspace_includes_workspace_info(self):
        binding = {
            "workspace_path": "/path/to/workspace",
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = None
        active_flow = None
        channel_id = "channel-123"

        lines = build_status_text(
            binding, collaboration_summary_lines, active_flow, channel_id
        )
        lines_str = "\n".join(lines)

        assert "Mode: workspace" in lines_str
        assert "Channel is bound." in lines_str
        assert "/path/to/workspace" in lines_str
        assert "my-repo" in lines_str
        assert "guild-1" in lines_str

    def test_bound_pma_includes_pma_info(self):
        binding = {
            "workspace_path": None,
            "repo_id": None,
            "guild_id": None,
            "pma_enabled": True,
            "pma_prev_workspace_path": "/prev/workspace",
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = None
        active_flow = None
        channel_id = "channel-123"

        lines = build_status_text(
            binding, collaboration_summary_lines, active_flow, channel_id
        )
        lines_str = "\n".join(lines)

        assert "Mode: PMA (hub)" in lines_str
        assert "/prev/workspace" in lines_str
        assert "Use /pma off to restore" in lines_str

    def test_includes_active_flow_info(self):
        binding = {
            "workspace_path": "/path/to/workspace",
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = None
        active_flow = ActiveFlowInfo(flow_id="run-123", status="running")
        channel_id = "channel-123"

        lines = build_status_text(
            binding, collaboration_summary_lines, active_flow, channel_id
        )
        lines_str = "\n".join(lines)

        assert "Active flow: run-123 (running)" in lines_str

    def test_includes_collaboration_summary(self):
        binding = {
            "workspace_path": "/path/to/workspace",
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = [
            "Policy destination: channel-123",
            "Policy mode: active",
        ]
        active_flow = None
        channel_id = "channel-123"

        lines = build_status_text(
            binding, collaboration_summary_lines, active_flow, channel_id
        )
        lines_str = "\n".join(lines)

        assert "Policy destination: channel-123" in lines_str
        assert "Policy mode: active" in lines_str

    def test_includes_flow_hint(self):
        binding = {
            "workspace_path": "/path/to/workspace",
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = None
        active_flow = None
        channel_id = "channel-123"

        lines = build_status_text(
            binding, collaboration_summary_lines, active_flow, channel_id
        )
        lines_str = "\n".join(lines)

        assert "Use /car flow status" in lines_str

    def test_excludes_flow_hint_when_disabled(self):
        binding = {
            "workspace_path": "/path/to/workspace",
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = None
        active_flow = None
        channel_id = "channel-123"

        lines = build_status_text(
            binding,
            collaboration_summary_lines,
            active_flow,
            channel_id,
            include_flow_hint=False,
        )
        lines_str = "\n".join(lines)

        assert "Use /car flow status" not in lines_str


class TestBuildDebugText:
    def test_unbound_channel_returns_basic_info(self):
        binding = None
        collaboration_summary_lines = None
        channel_id = "channel-123"

        lines = build_debug_text(binding, collaboration_summary_lines, channel_id)
        lines_str = "\n".join(lines)

        assert "Channel ID: channel-123" in lines_str
        assert "Binding: none (unbound)" in lines_str
        assert "Use /car bind path:<workspace>" in lines_str

    def test_bound_channel_includes_binding_info(self):
        binding = {
            "workspace_path": "/path/to/workspace",
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = None
        channel_id = "channel-123"

        lines = build_debug_text(binding, collaboration_summary_lines, channel_id)
        lines_str = "\n".join(lines)

        assert "Channel ID: channel-123" in lines_str
        assert "Guild ID: guild-1" in lines_str
        assert "Workspace: /path/to/workspace" in lines_str
        assert "PMA enabled: False" in lines_str

    def test_includes_workspace_path_resolution(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        binding = {
            "workspace_path": str(workspace),
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = None
        channel_id = "channel-123"

        lines = build_debug_text(binding, collaboration_summary_lines, channel_id)
        lines_str = "\n".join(lines)

        assert "Path exists: True" in lines_str
        assert ".codex-autorunner exists:" in lines_str

    def test_includes_pending_outbox_count(self):
        binding = {
            "workspace_path": "/path/to/workspace",
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = None
        channel_id = "channel-123"
        pending_outbox_count = 5

        lines = build_debug_text(
            binding, collaboration_summary_lines, channel_id, pending_outbox_count
        )
        lines_str = "\n".join(lines)

        assert "Pending outbox items: 5" in lines_str

    def test_includes_collaboration_summary(self):
        binding = {
            "workspace_path": "/path/to/workspace",
            "repo_id": "my-repo",
            "guild_id": "guild-1",
            "pma_enabled": False,
            "pma_prev_workspace_path": None,
            "updated_at": "2024-01-01",
        }
        collaboration_summary_lines = [
            "Policy mode: command_only",
            "Policy plain-text: disabled",
        ]
        channel_id = "channel-123"

        lines = build_debug_text(binding, collaboration_summary_lines, channel_id)
        lines_str = "\n".join(lines)

        assert "Policy mode: command_only" in lines_str
        assert "Policy plain-text: disabled" in lines_str


class TestBuildIdsText:
    def test_includes_basic_ids(self):
        channel_id = "channel-123"
        guild_id = "guild-456"
        user_id = "user-789"
        collaboration_summary_lines = None
        snippet_lines = None

        lines = build_ids_text(
            channel_id, guild_id, user_id, collaboration_summary_lines, snippet_lines
        )
        lines_str = "\n".join(lines)

        assert f"Channel ID: {channel_id}" in lines_str
        assert f"Guild ID: {guild_id}" in lines_str
        assert f"User ID: {user_id}" in lines_str
        assert f"discord_bot.allowed_channel_ids: [{channel_id}]" in lines_str
        assert f"discord_bot.allowed_guild_ids: [{guild_id}]" in lines_str
        assert f"discord_bot.allowed_user_ids: [{user_id}]" in lines_str

    def test_guild_id_none_excludes_guild_allowlist(self):
        channel_id = "channel-123"
        guild_id = None
        user_id = "user-789"
        collaboration_summary_lines = None
        snippet_lines = None

        lines = build_ids_text(
            channel_id, guild_id, user_id, collaboration_summary_lines, snippet_lines
        )
        lines_str = "\n".join(lines)

        assert f"Channel ID: {channel_id}" in lines_str
        assert "Guild ID: none" in lines_str
        assert "discord_bot.allowed_guild_ids" not in lines_str
        assert f"discord_bot.allowed_user_ids: [{user_id}]" in lines_str

    def test_user_id_none_excludes_user_allowlist(self):
        channel_id = "channel-123"
        guild_id = "guild-456"
        user_id = None
        collaboration_summary_lines = None
        snippet_lines = None

        lines = build_ids_text(
            channel_id, guild_id, user_id, collaboration_summary_lines, snippet_lines
        )
        lines_str = "\n".join(lines)

        assert f"Channel ID: {channel_id}" in lines_str
        assert "User ID: unknown" in lines_str
        assert "discord_bot.allowed_user_ids" not in lines_str

    def test_includes_collaboration_summary(self):
        channel_id = "channel-123"
        guild_id = "guild-456"
        user_id = "user-789"
        collaboration_summary_lines = [
            "Policy destination: channel-123",
            "Policy mode: active",
        ]
        snippet_lines = None

        lines = build_ids_text(
            channel_id, guild_id, user_id, collaboration_summary_lines, snippet_lines
        )
        lines_str = "\n".join(lines)

        assert "Policy destination: channel-123" in lines_str
        assert "Policy mode: active" in lines_str

    def test_includes_snippet_lines(self):
        channel_id = "channel-123"
        guild_id = "guild-456"
        user_id = "user-789"
        collaboration_summary_lines = None
        snippet_lines = [
            "Suggested collaboration config:",
            "collaboration_policy:",
            "  discord:",
        ]

        lines = build_ids_text(
            channel_id, guild_id, user_id, collaboration_summary_lines, snippet_lines
        )
        lines_str = "\n".join(lines)

        assert "Suggested collaboration config:" in lines_str
        assert "collaboration_policy:" in lines_str


class TestActiveFlowInfo:
    def test_accepts_running_status(self):
        flow = ActiveFlowInfo(flow_id="run-123", status="running")
        assert flow.flow_id == "run-123"
        assert flow.status == "running"

    def test_accepts_paused_status(self):
        flow = ActiveFlowInfo(flow_id="run-456", status="paused")
        assert flow.flow_id == "run-456"
        assert flow.status == "paused"
