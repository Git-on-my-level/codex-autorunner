from codex_autorunner.core.pma_thread_compaction import (
    build_managed_thread_compact_summary,
)


def test_build_managed_thread_compact_summary_uses_turn_outputs_only() -> None:
    summary = build_managed_thread_compact_summary(
        [
            {
                "prompt": "first user message",
                "assistant_text": "first assistant output",
                "status": "ok",
            },
            {
                "prompt": "second user message",
                "assistant_text": "second assistant output",
                "status": "ok",
            },
        ],
        max_chars=400,
    )

    assert summary is not None
    assert "Compact summary of recent managed thread turns:" in summary
    assert "User: first user message" in summary
    assert "Assistant: first assistant output" in summary
    assert "User: second user message" in summary
    assert "Assistant: second assistant output" in summary


def test_build_managed_thread_compact_summary_prefers_recent_turns_when_trimming() -> (
    None
):
    summary = build_managed_thread_compact_summary(
        [
            {
                "prompt": "older user message " * 20,
                "assistant_text": "older assistant output " * 20,
                "status": "ok",
            },
            {
                "prompt": "latest user message",
                "assistant_text": "latest assistant output",
                "status": "ok",
            },
        ],
        max_chars=180,
    )

    assert summary is not None
    assert "latest user message" in summary
    assert "latest assistant output" in summary
