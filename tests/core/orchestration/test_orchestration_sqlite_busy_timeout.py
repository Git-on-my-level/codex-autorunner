from codex_autorunner.core.orchestration.sqlite import (
    open_orchestration_sqlite,
    orchestration_sqlite_busy_timeout_ms,
)


def test_orchestration_busy_timeout_default():
    assert orchestration_sqlite_busy_timeout_ms() == 30_000


def test_orchestration_busy_timeout_constant():
    assert orchestration_sqlite_busy_timeout_ms() == 30_000


def test_open_orchestration_sqlite_applies_busy_timeout_pragma(tmp_path):
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None
        assert int(row[0]) == 30_000
