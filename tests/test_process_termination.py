from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from codex_autorunner.core.process_termination import terminate_record


def _assert_process_gone(pid: int) -> None:
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.1)
    pytest.fail(f"process {pid} still running after termination")


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX process groups")
def test_terminate_record_kills_sigterm_ignoring_process_group() -> None:
    parent_code = textwrap.dedent(
        """
        import os
        import signal
        import subprocess
        import sys
        import time

        def _ignore_signal(_sig, _frame):
            pass

        signal.signal(signal.SIGTERM, _ignore_signal)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(300)",
            ]
        )
        print(f"{os.getpid()} {child.pid} {os.getpgrp()}", flush=True)
        while True:
            time.sleep(1)
        """
    )

    process = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        line = process.stdout.readline()
        parent_pid_text, child_pid_text, pgid_text = line.strip().split()
        parent_pid = int(parent_pid_text)
        child_pid = int(child_pid_text)
        pgid = int(pgid_text)

        assert terminate_record(
            parent_pid,
            pgid,
            grace_seconds=0.4,
            kill_seconds=0.2,
        )

        process.wait(timeout=6)

        _assert_process_gone(parent_pid)
        _assert_process_gone(child_pid)
    finally:
        leader_pid = process.pid
        if process.poll() is None and leader_pid is not None:
            terminate_record(
                leader_pid,
                leader_pid,
                grace_seconds=0.1,
                kill_seconds=0.1,
            )
        process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()
