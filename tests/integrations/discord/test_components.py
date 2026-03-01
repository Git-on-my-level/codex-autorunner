from __future__ import annotations

from codex_autorunner.integrations.discord.components import (
    DISCORD_BUTTON_STYLE_DANGER,
    DISCORD_BUTTON_STYLE_SECONDARY,
    DISCORD_BUTTON_STYLE_SUCCESS,
    build_action_row,
    build_bind_picker,
    build_button,
    build_continue_turn_button,
    build_flow_runs_picker,
    build_flow_status_buttons,
    build_select_menu,
    build_select_option,
)


class TestBuildActionRow:
    def test_builds_action_row_with_components(self) -> None:
        button = build_button("Test", "test:click")
        row = build_action_row([button])
        assert row["type"] == 1
        assert len(row["components"]) == 1
        assert row["components"][0] == button


class TestBuildButton:
    def test_builds_button_with_defaults(self) -> None:
        button = build_button("Resume", "flow:123:resume")
        assert button["type"] == 2
        assert button["style"] == DISCORD_BUTTON_STYLE_SECONDARY
        assert button["label"] == "Resume"
        assert button["custom_id"] == "flow:123:resume"
        assert button["disabled"] is False

    def test_builds_button_with_custom_style(self) -> None:
        button = build_button(
            "Stop", "flow:123:stop", style=DISCORD_BUTTON_STYLE_DANGER
        )
        assert button["style"] == DISCORD_BUTTON_STYLE_DANGER


class TestBuildSelectMenu:
    def test_builds_select_menu(self) -> None:
        options = [
            build_select_option("Repo 1", "repo1"),
            build_select_option("Repo 2", "repo2"),
        ]
        menu = build_select_menu("bind_select", options, placeholder="Choose...")
        assert menu["type"] == 3
        assert menu["custom_id"] == "bind_select"
        assert menu["placeholder"] == "Choose..."
        assert len(menu["options"]) == 2

    def test_limits_options_to_25(self) -> None:
        options = [build_select_option(f"Opt{i}", f"val{i}") for i in range(30)]
        menu = build_select_menu("test", options)
        assert len(menu["options"]) == 25


class TestBuildSelectOption:
    def test_builds_option_with_description(self) -> None:
        option = build_select_option("my-repo", "my-repo", description="/path/to/repo")
        assert option["label"] == "my-repo"
        assert option["value"] == "my-repo"
        assert option["description"] == "/path/to/repo"
        assert option["default"] is False


class TestBuildBindPicker:
    def test_builds_picker_with_repos(self) -> None:
        repos = [("repo1", "/path/one"), ("repo2", "/path/two")]
        picker = build_bind_picker(repos)
        assert picker["type"] == 1
        menu = picker["components"][0]
        assert menu["type"] == 3
        assert menu["custom_id"] == "bind_select"
        assert len(menu["options"]) == 2

    def test_builds_picker_with_empty_repos(self) -> None:
        picker = build_bind_picker([])
        menu = picker["components"][0]
        assert len(menu["options"]) == 1
        assert menu["options"][0]["value"] == "none"


class TestBuildFlowStatusButtons:
    def test_paused_status_has_resume_and_archive(self) -> None:
        rows = build_flow_status_buttons("run-123", "paused")
        assert len(rows) == 2
        resume_row = rows[0]
        assert resume_row["components"][0]["label"] == "Resume"
        assert resume_row["components"][0]["style"] == DISCORD_BUTTON_STYLE_SUCCESS
        archive_row = rows[1]
        assert archive_row["components"][0]["label"] == "Archive"

    def test_running_status_has_stop_and_refresh(self) -> None:
        rows = build_flow_status_buttons("run-123", "running")
        assert len(rows) == 1
        buttons = rows[0]["components"]
        assert buttons[0]["label"] == "Stop"
        assert buttons[0]["style"] == DISCORD_BUTTON_STYLE_DANGER
        assert buttons[1]["label"] == "Refresh"

    def test_terminal_status_has_archive_and_refresh(self) -> None:
        for status in ["completed", "stopped", "failed"]:
            rows = build_flow_status_buttons("run-123", status)
            assert len(rows) == 1
            buttons = rows[0]["components"]
            assert buttons[0]["label"] == "Archive"
            assert buttons[1]["label"] == "Refresh"


class TestBuildFlowRunsPicker:
    def test_builds_picker_with_runs(self) -> None:
        runs = [("run-1", "running"), ("run-2", "paused")]
        picker = build_flow_runs_picker(runs)
        assert picker["type"] == 1
        menu = picker["components"][0]
        assert menu["custom_id"] == "flow_runs_select"
        assert len(menu["options"]) == 2

    def test_builds_picker_with_empty_runs(self) -> None:
        picker = build_flow_runs_picker([])
        menu = picker["components"][0]
        assert len(menu["options"]) == 1
        assert menu["options"][0]["value"] == "none"


class TestTurnButtons:
    def test_build_continue_turn_button(self) -> None:
        row = build_continue_turn_button()
        assert row["type"] == 1
        button = row["components"][0]
        assert button["label"] == "Continue"
        assert button["custom_id"] == "continue_turn"
        assert button["style"] == DISCORD_BUTTON_STYLE_SUCCESS
