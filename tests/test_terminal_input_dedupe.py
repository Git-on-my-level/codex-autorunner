import asyncio

import codex_autorunner.pty_session as pty_session
from codex_autorunner.pty_session import ActiveSession


def test_active_session_dedupes_input_ids():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    class DummyLoop:
        def add_reader(self, _fd, _cb):
            return None

    class DummyPTY:
        fd = 0

        def __init__(self):
            self.closed = False

    try:
        session = ActiveSession("s", DummyPTY(), DummyLoop())  # type: ignore[arg-type]
    finally:
        loop.close()

    assert session.mark_input_id_seen("a") is True
    assert session.mark_input_id_seen("a") is False
    assert session.mark_input_id_seen("b") is True


def test_active_session_buffers_when_write_blocks(monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    class DummyLoop:
        def __init__(self):
            self.writer_added = False
            self.writer_removed = False
            self.writer_cb = None

        def add_reader(self, _fd, _cb):
            return None

        def remove_reader(self, _fd):
            return None

        def add_writer(self, _fd, cb):
            self.writer_added = True
            self.writer_cb = cb

        def remove_writer(self, _fd):
            self.writer_removed = True
            self.writer_cb = None

    class DummyPTY:
        fd = 0

        def __init__(self):
            self.closed = False
            self.last_active = 0.0

    try:
        session = ActiveSession("s", DummyPTY(), DummyLoop())  # type: ignore[arg-type]

        writes = []

        def fake_write(_fd, data):
            writes.append(bytes(data))
            if len(writes) == 1:
                return 2
            raise BlockingIOError

        monkeypatch.setattr(pty_session.os, "write", fake_write)
        session.write_input(b"abcdef")

        assert session._pending_input == b"cdef"
        assert session._writer_active is True
        assert session.loop.writer_added is True

        monkeypatch.setattr(pty_session.os, "write", lambda _fd, data: len(data))
        session.loop.writer_cb()

        assert session._pending_input == b""
        assert session._writer_active is False
        assert session.loop.writer_removed is True
    finally:
        loop.close()
