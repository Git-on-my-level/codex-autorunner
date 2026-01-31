from codex_autorunner.core.review_context import build_spec_progress_review_context
from codex_autorunner.core.runtime import RuntimeContext


def test_review_context_includes_docs(repo) -> None:
    engine = RuntimeContext(repo)
    spec_path = engine.config.doc_path("spec")
    active_path = engine.config.doc_path("active_context")
    decisions_path = engine.config.doc_path("decisions")

    spec_path.write_text("Spec content", encoding="utf-8")
    active_path.write_text("Active context content", encoding="utf-8")
    decisions_path.write_text("Decisions content", encoding="utf-8")

    context = build_spec_progress_review_context(
        engine,
        exit_reason="todos_complete",
        last_run_id=None,
        last_exit_code=0,
        max_doc_chars=5000,
        primary_docs=["spec", "active_context"],
        include_docs=["decisions"],
        include_last_run_artifacts=False,
    )

    assert "Spec content" in context
    assert "Active context content" in context
    assert "decisions.md" in context
    assert "last_exit_code: 0" in context
