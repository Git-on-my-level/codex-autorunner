from codex_autorunner.core.engine import Engine


def test_engine_reads_run_log_file(repo):
    engine = Engine(repo)
    run_id = 3
    run_log = engine._run_log_path(run_id)
    run_log.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "=== run 3 start ===",
            "[2025-01-01T00:00:00Z] run=3 stdout: hello",
            "=== run 3 end (code 0) ===",
            "",
        ]
    )
    run_log.write_text(content, encoding="utf-8")
    assert engine.read_run_block(run_id) == content
    assert engine.extract_prev_output(run_id) == "hello"


def test_engine_builds_todo_snapshot(repo):
    engine = Engine(repo)
    before = "- [ ] Task one\n- [x] Done one\n"
    after = "- [ ] Task two\n- [x] Done one\n- [x] Done two\n"

    snapshot = engine._build_todo_snapshot(before, after)

    assert snapshot["before"]["outstanding"] == ["Task one"]
    assert snapshot["before"]["done"] == ["Done one"]
    assert snapshot["before"]["counts"]["outstanding"] == 1
    assert snapshot["after"]["outstanding"] == ["Task two"]
    assert snapshot["after"]["done"] == ["Done one", "Done two"]
    assert snapshot["after"]["counts"]["done"] == 2
