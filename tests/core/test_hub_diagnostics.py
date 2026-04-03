from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from io import StringIO
from pathlib import Path
from uuid import uuid4

from codex_autorunner.core.hub_diagnostics import (
    hub_clean_shutdown_path,
    hub_endpoint_path,
    hub_pid_path,
    install_hub_exception_hooks,
    read_hub_endpoint,
    record_hub_clean_shutdown,
    record_hub_startup,
)


def _make_buffer_logger() -> tuple[logging.Logger, StringIO, logging.Handler]:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger(f"test.hub_diagnostics.{uuid4()}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger, stream, handler


def _read_events(stream: StringIO) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in stream.getvalue().splitlines():
        raw = line.strip()
        if not raw.startswith("{"):
            continue
        events.append(json.loads(raw))
    return events


def test_record_hub_startup_logs_unclean_previous_run(tmp_path: Path) -> None:
    logger, stream, handler = _make_buffer_logger()
    pid_path = hub_pid_path(tmp_path)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("4321\n", encoding="utf-8")
    previous_started_at = 1_700_000_000
    os.utime(pid_path, (previous_started_at, previous_started_at))

    record_hub_startup(tmp_path, logger, pid=9876)
    handler.flush()

    assert pid_path.read_text(encoding="utf-8") == "9876\n"
    assert not hub_clean_shutdown_path(tmp_path).exists()

    events = _read_events(stream)
    assert events[0]["event"] == "hub_started_unclean"
    assert events[0]["pid"] == 9876
    assert events[0]["previous_pid"] == 4321
    assert events[0]["previous_started_at"] == "2023-11-14T22:13:20+00:00"
    assert events[1]["event"] == "hub_started"


def test_record_hub_startup_clears_clean_shutdown_marker(tmp_path: Path) -> None:
    logger, stream, handler = _make_buffer_logger()
    clean_shutdown_path = hub_clean_shutdown_path(tmp_path)
    clean_shutdown_path.parent.mkdir(parents=True, exist_ok=True)
    clean_shutdown_path.write_text("2026-04-02T00:00:00+00:00\n", encoding="utf-8")

    record_hub_startup(tmp_path, logger, pid=4321)
    handler.flush()

    assert not clean_shutdown_path.exists()
    events = _read_events(stream)
    assert [event["event"] for event in events] == ["hub_started"]


def test_record_hub_startup_writes_endpoint_file(tmp_path: Path) -> None:
    logger, _stream, _handler = _make_buffer_logger()

    record_hub_startup(
        tmp_path,
        logger,
        pid=4321,
        host="127.0.0.1",
        port=4517,
        base_path="/car",
    )

    payload = json.loads(hub_endpoint_path(tmp_path).read_text(encoding="utf-8"))
    assert payload == {
        "url": "http://127.0.0.1:4517/car",
        "host": "127.0.0.1",
        "port": 4517,
        "base_path": "/car",
    }


def test_read_hub_endpoint_returns_payload_when_pid_is_live(
    tmp_path: Path, monkeypatch
) -> None:
    logger, _stream, _handler = _make_buffer_logger()
    record_hub_startup(
        tmp_path,
        logger,
        pid=4321,
        host="127.0.0.1",
        port=4517,
        base_path="/car",
    )
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    assert read_hub_endpoint(tmp_path) == {
        "url": "http://127.0.0.1:4517/car",
        "host": "127.0.0.1",
        "port": 4517,
        "base_path": "/car",
    }


def test_read_hub_endpoint_returns_none_when_pid_is_stale(
    tmp_path: Path, monkeypatch
) -> None:
    logger, _stream, _handler = _make_buffer_logger()
    record_hub_startup(
        tmp_path,
        logger,
        pid=4321,
        host="127.0.0.1",
        port=4517,
        base_path="/car",
    )

    def _raise_stale(_pid: int, _sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", _raise_stale)

    assert read_hub_endpoint(tmp_path) is None


def test_record_hub_clean_shutdown_writes_marker(tmp_path: Path) -> None:
    logger, stream, handler = _make_buffer_logger()

    record_hub_clean_shutdown(tmp_path, logger, pid=4321)
    handler.flush()

    clean_shutdown_path = hub_clean_shutdown_path(tmp_path)
    assert clean_shutdown_path.exists()
    assert clean_shutdown_path.read_text(encoding="utf-8").strip()
    events = _read_events(stream)
    assert len(events) == 1
    assert events[0]["event"] == "hub_shutdown_clean"
    assert events[0]["pid"] == 4321
    assert events[0]["shutdown_at"]


def test_record_hub_clean_shutdown_removes_endpoint_file(tmp_path: Path) -> None:
    logger, _stream, _handler = _make_buffer_logger()
    endpoint_path = hub_endpoint_path(tmp_path)
    endpoint_path.parent.mkdir(parents=True, exist_ok=True)
    endpoint_path.write_text("{}", encoding="utf-8")

    record_hub_clean_shutdown(tmp_path, logger, pid=4321)

    assert endpoint_path.exists() is False


def test_install_hub_exception_hooks_logs_uncaught_sys_exception(
    monkeypatch,
) -> None:
    logger, stream, handler = _make_buffer_logger()
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(sys, "excepthook", lambda *_args: None)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    hooks = install_hub_exception_hooks(logger=logger, loop=loop)

    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
    finally:
        hooks.restore()
        loop.close()
    handler.flush()

    events = _read_events(stream)
    assert events[0]["event"] == "hub_uncaught_exception"
    assert events[0]["source"] == "sys"
    assert events[0]["error"] == "boom"
    assert "Unhandled hub exception" in stream.getvalue()
