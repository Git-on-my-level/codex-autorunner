from codex_autorunner.core.flows.models import FlowEventType
from codex_autorunner.core.flows.store import FlowStore
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


def test_review_context_reads_latest_flowstore_artifacts(repo) -> None:
    engine = RuntimeContext(repo)
    spec_path = engine.config.doc_path("spec")
    active_path = engine.config.doc_path("active_context")
    spec_path.write_text("Spec", encoding="utf-8")
    active_path.write_text("Active", encoding="utf-8")

    artifact_path = repo / "artifact-output.txt"
    artifact_path.write_text("flow artifact payload", encoding="utf-8")

    db_path = repo / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path, durable=engine.config.durable_writes) as store:
        store.create_flow_run(
            run_id="flow-run-1",
            flow_type="ticket_flow",
            input_data={"ticket": "TICKET-001"},
        )
        store.create_event(
            event_id="evt-1",
            run_id="flow-run-1",
            event_type=FlowEventType.STEP_PROGRESS,
            data={"current_ticket": "TICKET-001", "status": "running"},
        )
        store.create_artifact(
            artifact_id="art-1",
            run_id="flow-run-1",
            kind="output",
            path=str(artifact_path),
        )

    context = build_spec_progress_review_context(
        engine,
        exit_reason="todos_complete",
        last_run_id=None,
        last_exit_code=0,
        max_doc_chars=5000,
        primary_docs=["spec", "active_context"],
        include_docs=[],
        include_last_run_artifacts=True,
    )

    assert "Flow run summary" in context
    assert "run_id: flow-run-1" in context
    assert "step_progress" in context
    assert "Output" in context
    assert "flow artifact payload" in context
