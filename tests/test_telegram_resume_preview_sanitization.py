from codex_autorunner.integrations.telegram.helpers import (
    _extract_first_user_preview,
    _extract_thread_preview_parts,
    _format_resume_summary,
)


def test_extract_thread_preview_parts_strips_injected_context_from_user_preview() -> (
    None
):
    user_preview, assistant_preview = _extract_thread_preview_parts(
        {
            "last_user_message": (
                "<injected context>\n"
                "You are operating inside a Codex Autorunner (CAR) managed repo.\n"
                "</injected context>\n\n"
                "Resume this thread with useful labels."
            ),
            "last_assistant_message": "Sure, I can do that.",
        }
    )
    assert user_preview == "Resume this thread with useful labels."
    assert assistant_preview == "Sure, I can do that."


def test_extract_first_user_preview_strips_injected_context_blocks() -> None:
    preview = _extract_first_user_preview(
        {
            "first_user_message": (
                "<injected context>\n"
                "workspace details here\n"
                "</injected context>\n\n"
                "Fix resume picker text."
            )
        }
    )
    assert preview == "Fix resume picker text."


def test_extract_first_user_preview_strips_leading_car_html_comments() -> None:
    preview = _extract_first_user_preview(
        {
            "first_user_message": (
                "<!-- CAR:PMA_DOCS_GENERATED -->\n\n" "Show the session datetime first."
            )
        }
    )
    assert preview == "Show the session datetime first."


def test_format_resume_summary_preserves_default_model_and_effort_labels() -> None:
    summary = _format_resume_summary(
        "thread-1",
        {
            "last_user_message": "Resume this session.",
            "last_assistant_message": "Ready.",
        },
        workspace_path="/repo",
        model=None,
        effort=None,
    )

    assert "Resumed thread `thread-1`" in summary
    assert "Directory: /repo" in summary
    assert "Model: default" in summary
    assert "Effort: default" in summary
