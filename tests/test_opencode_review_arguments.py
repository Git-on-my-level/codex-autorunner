import codex_autorunner.integrations.telegram.handlers.commands.command_utils as command_utils


def test_opencode_review_arguments_uncommitted() -> None:
    assert (
        command_utils._opencode_review_arguments({"type": "uncommittedChanges"}) == ""
    )


def test_opencode_review_arguments_base_branch() -> None:
    assert (
        command_utils._opencode_review_arguments(
            {"type": "baseBranch", "branch": "main"}
        )
        == "main"
    )


def test_opencode_review_arguments_commit() -> None:
    assert (
        command_utils._opencode_review_arguments({"type": "commit", "sha": "abc123"})
        == "abc123"
    )


def test_opencode_review_arguments_custom_instructions() -> None:
    args = command_utils._opencode_review_arguments(
        {"type": "custom", "instructions": "focus on security"}
    )
    assert "uncommitted" in args
    assert "focus on security" in args
