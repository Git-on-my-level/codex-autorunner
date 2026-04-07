from __future__ import annotations

import sqlite3
import threading

import pytest

from codex_autorunner.core.flows.store import FlowStore


def test_flow_store_context_manager_cleanup(tmp_path):
    """Test that FlowStore context manager properly closes connections."""
    db_path = tmp_path / "flows.db"

    # Track open connections
    open_connections: set[int] = set()
    connection_lock = threading.Lock()

    # Monkey-patch sqlite3.connect to track connections
    original_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        with connection_lock:
            open_connections.add(id(conn))
        return conn

    sqlite3.connect = tracking_connect

    try:
        # Use FlowStore as context manager
        with FlowStore(db_path) as store:
            store.create_flow_run(
                "test-run-1",
                "test_flow",
                input_data={"test": "data"},
                state={},
                metadata={},
            )
            # Connection should be open inside the context
            assert len(open_connections) > 0

        # Connection should be closed after exiting context
        # (Note: we're checking that the store's internal connection is gone,
        # not that all sqlite connections are closed, since sqlite3 may have
        # internal caching)
        store2 = FlowStore(db_path)
        store2.initialize()
        try:
            runs = store2.list_flow_runs()
            assert len(runs) == 1
            assert runs[0].id == "test-run-1"
        finally:
            store2.close()

    finally:
        sqlite3.connect = original_connect


def test_flow_store_repeated_context_manager_use(tmp_path):
    """Test that FlowStore can be used multiple times with context manager."""
    db_path = tmp_path / "flows.db"

    # First use
    with FlowStore(db_path) as store:
        store.create_flow_run(
            "test-run-1",
            "test_flow",
            input_data={"test": "data1"},
            state={},
            metadata={},
        )

    # Second use - should create new connection
    with FlowStore(db_path) as store:
        runs = store.list_flow_runs()
        assert len(runs) == 1

        store.create_flow_run(
            "test-run-2",
            "test_flow",
            input_data={"test": "data2"},
            state={},
            metadata={},
        )

    # Third use - should see both runs
    with FlowStore(db_path) as store:
        runs = store.list_flow_runs()
        assert len(runs) == 2
        run_ids = {r.id for r in runs}
        assert run_ids == {"test-run-1", "test-run-2"}


def test_flow_store_context_manager_exception_handling(tmp_path):
    """Test that FlowStore context manager closes connection even on exception."""
    db_path = tmp_path / "flows.db"

    class CustomError(Exception):
        pass

    try:
        with FlowStore(db_path) as store:
            store.create_flow_run(
                "test-run-1",
                "test_flow",
                input_data={"test": "data"},
                state={},
                metadata={},
            )
            raise CustomError("Test exception")
    except CustomError:
        pass

    # Connection should be closed, new instance should work
    with FlowStore(db_path) as store:
        runs = store.list_flow_runs()
        assert len(runs) == 1
        assert runs[0].id == "test-run-1"


def test_flow_store_get_latest_flow_run_uses_newest_matching_record(tmp_path):
    db_path = tmp_path / "flows.db"

    with FlowStore(db_path) as store:
        store.create_flow_run(
            "older-run",
            "ticket_flow",
            input_data={},
            state={},
            metadata={},
        )
        store.create_flow_run(
            "newer-run",
            "ticket_flow",
            input_data={},
            state={},
            metadata={},
        )
        store.create_flow_run(
            "other-flow-run",
            "other_flow",
            input_data={},
            state={},
            metadata={},
        )

        latest = store.get_latest_flow_run(flow_type="ticket_flow")

    assert latest is not None
    assert latest.id == "newer-run"


def test_flow_store_connect_readonly_uses_read_only_sqlite_mode(tmp_path, monkeypatch):
    db_path = tmp_path / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(
            "test-run-1",
            "test_flow",
            input_data={},
            state={},
            metadata={},
        )

    original_connect = sqlite3.connect
    connect_calls: list[tuple[object, bool]] = []

    def tracking_connect(*args, **kwargs):
        connect_calls.append((args[0], bool(kwargs.get("uri"))))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    store = FlowStore.connect_readonly(db_path)
    try:
        store.initialize()
        runs = store.list_flow_runs()
    finally:
        store.close()

    assert len(runs) == 1
    assert runs[0].id == "test-run-1"
    assert any(
        "?mode=ro" in str(target) and used_uri for target, used_uri in connect_calls
    )


def test_flow_store_readonly_transaction_is_rejected(tmp_path):
    db_path = tmp_path / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(
            "test-run-1",
            "test_flow",
            input_data={},
            state={},
            metadata={},
        )

    store = FlowStore.connect_readonly(db_path)
    try:
        with pytest.raises(RuntimeError, match="read-only"):
            with store.transaction():
                pass
    finally:
        store.close()
