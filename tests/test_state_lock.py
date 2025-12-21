import threading
import time
from pathlib import Path

from codex_autorunner.state import state_lock


def test_state_lock_blocks_until_release(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    entered = threading.Event()
    ready = threading.Event()

    def attempt_lock() -> None:
        ready.set()
        with state_lock(state_path):
            entered.set()

    with state_lock(state_path):
        thread = threading.Thread(target=attempt_lock)
        thread.start()
        ready.wait(timeout=1.0)
        time.sleep(0.1)
        assert not entered.is_set()

    thread.join(timeout=1.0)
    assert entered.is_set()
