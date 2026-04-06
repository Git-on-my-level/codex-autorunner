from codex_autorunner.core.orchestration.sqlite import (
    open_orchestration_sqlite,
    orchestration_sqlite_busy_timeout_ms,
)


def test_orchestration_busy_timeout_default(monkeypatch):
    monkeypatch.delenv("CAR_ORCHESTRATION_SQLITE_BUSY_TIMEOUT_MS", raising=False)
    assert orchestration_sqlite_busy_timeout_ms() == 30_000


def test_orchestration_busy_timeout_env(monkeypatch):
    monkeypatch.setenv("CAR_ORCHESTRATION_SQLITE_BUSY_TIMEOUT_MS", "12345")
    assert orchestration_sqlite_busy_timeout_ms() == 12_345


def test_open_orchestration_sqlite_applies_busy_timeout_pragma(tmp_path, monkeypatch):
    monkeypatch.setenv("CAR_ORCHESTRATION_SQLITE_BUSY_TIMEOUT_MS", "7777")
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None
        assert int(row[0]) == 7777
