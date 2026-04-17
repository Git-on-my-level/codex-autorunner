from __future__ import annotations

import json
import time

from codex_autorunner.core.diagnostics.loop_attribution import (
    get_loop_names,
    reset_loop_attribution,
    snapshot_loop_attribution,
    track_loop,
)


def test_track_loop_records_idle_wakeup():
    reset_loop_attribution()

    with track_loop("test.loop_a"):
        pass

    snap = snapshot_loop_attribution()
    assert "test.loop_a" in snap["loops"]
    entry = snap["loops"]["test.loop_a"]
    assert entry["wakeups"] == 1
    assert entry["idle_wakeups"] == 1
    assert entry["productive_wakeups"] == 0
    assert entry["disk_reads"] == 0
    assert entry["db_reads"] == 0


def test_track_loop_records_productive_wakeup():
    reset_loop_attribution()

    with track_loop("test.loop_b") as scope:
        scope.mark_productive()
        scope.record_disk_read(2)
        scope.record_db_read(1)

    snap = snapshot_loop_attribution()
    entry = snap["loops"]["test.loop_b"]
    assert entry["wakeups"] == 1
    assert entry["productive_wakeups"] == 1
    assert entry["idle_wakeups"] == 0
    assert entry["disk_reads"] == 2
    assert entry["db_reads"] == 1


def test_track_loop_accumulates_across_iterations():
    reset_loop_attribution()

    for i in range(5):
        with track_loop("test.loop_c") as scope:
            if i % 2 == 0:
                scope.mark_productive()

    snap = snapshot_loop_attribution()
    entry = snap["loops"]["test.loop_c"]
    assert entry["wakeups"] == 5
    assert entry["productive_wakeups"] == 3
    assert entry["idle_wakeups"] == 2


def test_snapshot_includes_timestamp():
    reset_loop_attribution()

    before = time.time()
    snap = snapshot_loop_attribution()
    after = time.time()

    assert "snapshot_at" in snap
    assert before <= snap["snapshot_at"] <= after


def test_snapshot_empty_when_no_loops_registered():
    reset_loop_attribution()

    snap = snapshot_loop_attribution()
    assert snap["loops"] == {}


def test_reset_clears_all_counters():
    reset_loop_attribution()

    with track_loop("test.loop_d"):
        pass

    reset_loop_attribution()
    snap = snapshot_loop_attribution()
    assert snap["loops"] == {}


def test_get_loop_names():
    reset_loop_attribution()

    with track_loop("test.alpha"):
        pass
    with track_loop("test.beta"):
        pass

    names = get_loop_names()
    assert names == ["test.alpha", "test.beta"]


def test_avg_wakeup_duration():
    reset_loop_attribution()

    with track_loop("test.duration"):
        time.sleep(0.01)

    snap = snapshot_loop_attribution()
    entry = snap["loops"]["test.duration"]
    assert entry["avg_wakeup_duration_seconds"] > 0


def test_wakeup_scope_tracks_multiple_reads():
    reset_loop_attribution()

    with track_loop("test.multi_read") as scope:
        scope.record_disk_read(3)
        scope.record_db_read(2)
        scope.record_disk_read(1)

    snap = snapshot_loop_attribution()
    entry = snap["loops"]["test.multi_read"]
    assert entry["disk_reads"] == 4
    assert entry["db_reads"] == 2


def test_snapshot_durable_across_runs():
    reset_loop_attribution()

    with track_loop("test.run1"):
        pass

    snap1 = snapshot_loop_attribution()

    with track_loop("test.run1") as scope:
        scope.mark_productive()

    snap2 = snapshot_loop_attribution()

    assert snap1["loops"]["test.run1"]["wakeups"] == 1
    assert snap1["loops"]["test.run1"]["idle_wakeups"] == 1
    assert snap2["loops"]["test.run1"]["wakeups"] == 2
    assert snap2["loops"]["test.run1"]["productive_wakeups"] == 1


def test_smoke_soak_artifact_includes_attribution():
    reset_loop_attribution()

    with track_loop("smoke.loop_a") as scope:
        scope.mark_productive()
        scope.record_db_read(1)

    with track_loop("smoke.loop_b"):
        pass

    with track_loop("smoke.loop_b"):
        pass

    artifact = snapshot_loop_attribution()

    assert "loops" in artifact
    assert "snapshot_at" in artifact
    assert len(artifact["loops"]) == 2

    a = artifact["loops"]["smoke.loop_a"]
    assert a["wakeups"] == 1
    assert a["productive_wakeups"] == 1
    assert a["db_reads"] == 1

    b = artifact["loops"]["smoke.loop_b"]
    assert b["wakeups"] == 2
    assert b["idle_wakeups"] == 2

    serialized = json.dumps(artifact)
    assert "smoke.loop_a" in serialized
    assert "smoke.loop_b" in serialized
