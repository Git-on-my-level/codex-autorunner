import asyncio

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
