from __future__ import annotations

from types import SimpleNamespace

from codex_autorunner.integrations.chat.agents import CHAT_AGENT_DEFINITIONS
from codex_autorunner.integrations.chat.model_selection import REASONING_EFFORT_VALUES
from codex_autorunner.integrations.discord.components import (
    DISCORD_BUTTON_STYLE_DANGER,
    DISCORD_BUTTON_STYLE_SECONDARY,
    DISCORD_BUTTON_STYLE_SUCCESS,
    build_action_row,
    build_agent_picker,
    build_agent_profile_picker,
    build_bind_picker,
    build_button,
    build_flow_runs_picker,
    build_flow_status_buttons,
    build_model_effort_picker,
    build_model_picker,
    build_review_commit_picker,
    build_select_menu,
    build_select_option,
    build_session_threads_picker,
    build_update_target_picker,
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

    def test_builds_picker_with_explicit_bind_entries(self) -> None:
        picker = build_bind_picker(
            [
                ("repo-token", "repo-a", "/path/one"),
                ("/path/two", "path-two", "/path/two"),
            ]
        )
        menu = picker["components"][0]
        assert menu["options"][0]["value"] == "repo-token"
        assert menu["options"][0]["label"] == "repo-a"
        assert menu["options"][0]["description"] == "/path/one"
        assert menu["options"][1]["value"] == "/path/two"
        assert menu["options"][1]["label"] == "path-two"

    def test_builds_picker_with_empty_repos(self) -> None:
        picker = build_bind_picker([])
        menu = picker["components"][0]
        assert len(menu["options"]) == 1
        assert menu["options"][0]["value"] == "none"


class TestBuildAgentPicker:
    def test_builds_picker_with_current_agent_selected(self) -> None:
        picker = build_agent_picker(current_agent="opencode")
        menu = picker["components"][0]
        assert menu["custom_id"] == "agent_select"
        assert [opt["value"] for opt in menu["options"]] == [
            definition.value for definition in CHAT_AGENT_DEFINITIONS
        ]
        codex = menu["options"][0]
        opencode = menu["options"][1]
        assert codex["default"] is False
        assert opencode["default"] is True

    def test_does_not_append_registered_hermes_aliases(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "codex_autorunner.agents.registry.get_registered_agents",
            lambda context=None: {
                "hermes-m4-pma": SimpleNamespace(name="Hermes (hermes-m4-pma)"),
            },
        )

        picker = build_agent_picker(current_agent="hermes", context="repo-root")
        menu = picker["components"][0]
        values = [opt["value"] for opt in menu["options"]]

        assert "hermes-m4-pma" not in values
        assert menu["options"][2]["value"] == "hermes"
        assert menu["options"][2]["default"] is True


class TestBuildAgentProfilePicker:
    def test_builds_picker_with_default_and_profiles(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "codex_autorunner.agents.registry.get_registered_agents",
            lambda context=None: {
                "hermes-m4-pma": SimpleNamespace(name="Hermes (hermes-m4-pma)"),
            },
        )

        picker = build_agent_profile_picker(
            current_profile="m4-pma",
            context="repo-root",
        )
        menu = picker["components"][0]

        assert menu["custom_id"] == "agent_profile_select"
        assert menu["options"][0]["value"] == "clear"
        assert menu["options"][1]["value"] == "m4-pma"
        assert menu["options"][1]["default"] is True


class TestBuildModelPicker:
    def test_builds_picker_with_clear_option_and_models(self) -> None:
        picker = build_model_picker(
            [("gpt-5.3-codex", "gpt-5.3-codex"), ("openai/gpt-4o", "openai/gpt-4o")],
            current_model="gpt-5.3-codex",
        )
        menu = picker["components"][0]
        assert menu["custom_id"] == "model_select"
        assert menu["options"][0]["value"] == "clear"
        assert menu["options"][0]["default"] is False
        assert menu["options"][1]["value"] == "gpt-5.3-codex"
        assert menu["options"][1]["default"] is True
        assert menu["options"][2]["value"] == "openai/gpt-4o"

    def test_includes_current_model_even_when_not_in_first_page(self) -> None:
        models = [(f"model-{i}", f"model-{i}") for i in range(40)]
        picker = build_model_picker(models, current_model="special/current-model")
        menu = picker["components"][0]
        options = menu["options"]
        assert len(options) == 25
        current = options[-1]
        assert current["value"] == "special/current-model"
        assert current["default"] is True


class TestBuildModelEffortPicker:
    def test_builds_effort_picker(self) -> None:
        picker = build_model_effort_picker()
        menu = picker["components"][0]
        assert menu["custom_id"] == "model_effort_select"
        values = [opt["value"] for opt in menu["options"]]
        assert values == list(REASONING_EFFORT_VALUES)


class TestBuildFlowStatusButtons:
    def test_paused_status_has_resume_restart_and_archive(self) -> None:
        rows = build_flow_status_buttons("run-123", "paused")
        assert len(rows) == 2
        resume_row = rows[0]
        assert resume_row["components"][0]["label"] == "Resume"
        assert resume_row["components"][0]["style"] == DISCORD_BUTTON_STYLE_SUCCESS
        assert resume_row["components"][1]["label"] == "Restart"
        archive_row = rows[1]
        assert archive_row["components"][0]["label"] == "Archive"

    def test_running_status_has_stop_and_refresh(self) -> None:
        rows = build_flow_status_buttons("run-123", "running")
        assert len(rows) == 1
        buttons = rows[0]["components"]
        assert buttons[0]["label"] == "Stop"
        assert buttons[0]["style"] == DISCORD_BUTTON_STYLE_DANGER
        assert buttons[1]["label"] == "Refresh"

    def test_terminal_status_has_restart_archive_and_refresh(self) -> None:
        for status in ["completed", "stopped", "failed"]:
            rows = build_flow_status_buttons("run-123", status)
            assert len(rows) == 1
            buttons = rows[0]["components"]
            assert buttons[0]["label"] == "Restart"
            assert buttons[1]["label"] == "Archive"
            assert buttons[2]["label"] == "Refresh"


class TestBuildFlowRunsPicker:
    def test_builds_picker_with_runs(self) -> None:
        runs = [("run-1", "running"), ("run-2", "paused")]
        picker = build_flow_runs_picker(runs)
        assert picker["type"] == 1
        menu = picker["components"][0]
        assert menu["custom_id"] == "flow_runs_select"
        assert len(menu["options"]) == 2

    def test_marks_current_run_as_selected(self) -> None:
        runs = [("run-1", "running"), ("run-2", "paused")]
        picker = build_flow_runs_picker(runs, current_run_id="run-2")
        options = picker["components"][0]["options"]
        assert options[0]["default"] is False
        assert options[1]["default"] is True

    def test_builds_picker_with_empty_runs(self) -> None:
        picker = build_flow_runs_picker([])
        menu = picker["components"][0]
        assert len(menu["options"]) == 1
        assert menu["options"][0]["value"] == "none"


class TestBuildSessionThreadsPicker:
    def test_builds_picker_with_threads(self) -> None:
        picker = build_session_threads_picker(
            [("th-1", "th-1 (current)"), ("th-2", "th-2")]
        )
        menu = picker["components"][0]
        assert menu["custom_id"] == "session_resume_select"
        assert len(menu["options"]) == 2
        assert menu["options"][0]["value"] == "th-1"


class TestBuildReviewCommitPicker:
    def test_builds_commit_picker(self) -> None:
        picker = build_review_commit_picker(
            [("abcdef123456", "Fix tests"), ("0123456789ab", "Refactor")]
        )
        menu = picker["components"][0]
        assert menu["custom_id"] == "review_commit_select"
        assert len(menu["options"]) == 2
        assert menu["options"][0]["value"] == "abcdef123456"


class TestBuildUpdateTargetPicker:
    def test_builds_update_target_picker(self) -> None:
        picker = build_update_target_picker()
        menu = picker["components"][0]
        assert menu["custom_id"] == "update_target_select"
        values = {opt["value"] for opt in menu["options"]}
        assert values == {"all", "web", "chat", "telegram", "discord", "status"}
        all_option = next(opt for opt in menu["options"] if opt["value"] == "all")
        assert all_option["label"] == "all"
