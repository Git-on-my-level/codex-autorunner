from codex_autorunner.integrations.chat.session_messages import (
    build_branch_reset_started_lines,
    build_fresh_session_started_lines,
    build_reset_state_lines,
    build_resumed_thread_lines,
    build_thread_detail_lines,
    format_resumed_session_message,
    format_update_preparing_message,
    format_update_started_message,
    format_update_status_message,
)
from codex_autorunner.integrations.chat.update_notifier import (
    format_update_status_message as format_update_status_message_from_notifier,
)


class TestSessionMessages:
    def test_build_fresh_session_started_lines(self) -> None:
        assert build_fresh_session_started_lines(
            mode_label="PMA",
            actor_label="hermes [m4-pma]",
            state_label="new thread ready",
        ) == ["Started a fresh PMA session for `hermes [m4-pma]` (new thread ready)."]

    def test_build_branch_reset_started_lines(self) -> None:
        assert build_branch_reset_started_lines(
            branch_name="thread-discord-123",
            default_branch="main",
            mode_label="repo",
            actor_label="codex",
            state_label="cleared previous thread",
            setup_command_count=2,
        ) == [
            "Reset branch `thread-discord-123` to `origin/main` in current workspace and started fresh repo session for `codex` (cleared previous thread). Ran 2 setup command(s)."
        ]

    def test_build_reset_state_lines(self) -> None:
        assert build_reset_state_lines(
            mode_label="PMA",
            actor_label="hermes",
            state_label="fresh state",
        ) == ["Reset PMA thread state (fresh state) for `hermes`."]

    def test_build_thread_detail_lines(self) -> None:
        assert build_thread_detail_lines(
            thread_id="thread-1",
            workspace_path="/repo",
            actor_label="codex",
            model="gpt-5.4",
            effort="high",
            headline="Started new thread `thread-1`.",
        ) == [
            "Started new thread `thread-1`.",
            "Directory: /repo",
            "Agent: codex",
            "Model: gpt-5.4",
            "Effort: high",
        ]

    def test_build_resumed_thread_lines(self) -> None:
        assert build_resumed_thread_lines(
            thread_id="thread-1",
            workspace_path="/repo",
            actor_label="codex",
            model="gpt-5.4",
            effort="high",
            user_preview="User preview",
            assistant_preview="Assistant preview",
        ) == [
            "Resumed thread `thread-1`",
            "Directory: /repo",
            "Agent: codex",
            "Model: gpt-5.4",
            "Effort: high",
            "",
            "User:",
            "User preview",
            "",
            "Assistant:",
            "Assistant preview",
        ]

    def test_format_resumed_session_message(self) -> None:
        assert (
            format_resumed_session_message(
                mode_label="repo",
                actor_label="codex",
                thread_id="thread-1",
            )
            == "Resumed repo session for `codex` with thread `thread-1`."
        )

    def test_format_update_preparing_message(self) -> None:
        assert (
            format_update_preparing_message(
                "chat",
                restart_required=True,
                status_command="`/car update target:status`",
                completion_scope_label="this channel",
            )
            == "Preparing update (chat). Checking whether the update can start now. If it does, the selected service(s) will restart shortly and I will post completion status in this channel. Use `/car update target:status` for progress."
        )

    def test_format_update_started_message(self) -> None:
        assert (
            format_update_started_message(
                "web",
                restart_required=False,
                completion_scope_label="this thread",
            )
            == "Update started (web). I will post completion status in this thread. Use /update status for progress."
        )

    def test_format_update_status_message_delegates_to_shared_notifier(self) -> None:
        status = {
            "status": "running",
            "message": "Working",
            "update_target": "discord",
            "at": 1700000000,
        }

        assert format_update_status_message(
            status
        ) == format_update_status_message_from_notifier(status)

    def test_format_update_status_message_appends_repo_and_log_details(self) -> None:
        status = {
            "status": "ok",
            "message": "Done",
            "update_target": "discord",
            "at": 1700000000,
            "repo_ref": "main",
            "log_path": "/tmp/update.log",
        }

        assert format_update_status_message(status).endswith(
            "Ref: main\nLog: /tmp/update.log"
        )
