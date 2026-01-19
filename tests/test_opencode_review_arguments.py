import codex_autorunner.integrations.telegram.handlers.commands_runtime as commands_runtime


def test_opencode_review_arguments_uncommitted() -> None:
    assert (
        commands_runtime._opencode_review_arguments({"type": "uncommittedChanges"})
        == ""
    )


def test_opencode_review_arguments_base_branch() -> None:
    assert (
        commands_runtime._opencode_review_arguments(
            {"type": "baseBranch", "branch": "main"}
        )
        == "main"
    )


def test_opencode_review_arguments_commit() -> None:
    assert (
        commands_runtime._opencode_review_arguments({"type": "commit", "sha": "abc123"})
        == "abc123"
    )


def test_opencode_review_arguments_custom_instructions() -> None:
    args = commands_runtime._opencode_review_arguments(
        {"type": "custom", "instructions": "focus on security"}
    )
    assert "uncommitted" in args
    assert "focus on security" in args
