from codex_autorunner.core.engine import Engine
from codex_autorunner.core.review_context import build_spec_progress_review_context


def test_review_context_includes_docs_and_artifacts(repo) -> None:
    engine = Engine(repo)
    spec_path = engine.config.doc_path("spec")
    active_path = engine.config.doc_path("active_context")
    decisions_path = engine.config.doc_path("decisions")

    spec_path.write_text("Spec content", encoding="utf-8")
    active_path.write_text("Active context content", encoding="utf-8")
    decisions_path.write_text("Decisions content", encoding="utf-8")

    artifact_path = engine.log_path.parent / "runs" / "run-1.output.txt"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("artifact output", encoding="utf-8")
    engine._merge_run_index_entry(1, {"artifacts": {"output_path": str(artifact_path)}})

    context = build_spec_progress_review_context(
        engine,
        exit_reason="todos_complete",
        last_run_id=1,
        last_exit_code=0,
        max_doc_chars=5000,
        primary_docs=["spec", "active_context"],
        include_docs=["decisions"],
        include_last_run_artifacts=True,
    )

    assert "Spec content" in context
    assert "Active context content" in context
    assert "decisions.md" in context
    assert "artifact output" in context
    assert "last_exit_code: 0" in context
