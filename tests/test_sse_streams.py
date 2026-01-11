import asyncio
import time

from codex_autorunner.core.engine import Engine
from codex_autorunner.routes.shared import state_stream


def test_state_stream_emits_heartbeat(repo) -> None:
    engine = Engine(repo)
    generator = state_stream(engine, None, heartbeat_interval=1.0)

    async def _read() -> None:
        try:
            first = await generator.__anext__()
            assert first.startswith("data:")
            start = time.time()
            while True:
                event = await generator.__anext__()
                if event.startswith(": ping"):
                    return
                if time.time() - start > 3:
                    raise AssertionError("heartbeat not emitted")
        finally:
            await generator.aclose()

    asyncio.run(_read())
