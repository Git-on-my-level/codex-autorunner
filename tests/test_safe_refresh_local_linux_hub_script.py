from __future__ import annotations

from pathlib import Path


def test_safe_refresh_local_linux_hub_script_records_phase_timing() -> None:
    script = Path("scripts/safe-refresh-local-linux-hub.sh").read_text(encoding="utf-8")

    assert '"event": "update.phase_timing"' in script
    assert '"phase_timings"' in script
    assert "run_timed_phase() {" in script
    assert 'run_timed_phase "pip_install"' in script
    assert 'run_timed_phase "managed_repo_refresh"' in script
    assert 'run_timed_phase "hub_restart"' in script
    assert 'run_timed_phase "hub_health_check"' in script


def test_safe_refresh_local_linux_hub_script_preserves_timing_status() -> None:
    script = Path("scripts/safe-refresh-local-linux-hub.sh").read_text(encoding="utf-8")

    write_status_body = script.split("write_status() {", 1)[1].split("\n}\n", 1)[0]
    assert '"phase_timings"' in write_status_body
    assert '"last_phase_timing"' in write_status_body
