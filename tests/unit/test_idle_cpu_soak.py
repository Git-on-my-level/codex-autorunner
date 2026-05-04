from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOAK_SCRIPT = _REPO_ROOT / "scripts" / "idle_cpu_soak.py"
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import idle_cpu_soak as soak_module  # noqa: E402


class TestIdleCpuSoakArtifactSchema:
    def test_artifact_has_required_keys(self):
        from codex_autorunner.core.diagnostics.cpu_sampler import (
            CpuSample,
            aggregate_samples,
            evaluate_signoff,
        )

        samples = [
            CpuSample(
                aggregate_cpu_percent=1.0,
                aggregate_rss_mb=30.0,
                per_process=[
                    {
                        "pid": 100,
                        "command": "car hub serve",
                        "category": "car_service",
                        "cpu_percent": 1.0,
                        "rss_mb": 30.0,
                    }
                ],
            )
        ]
        agg = aggregate_samples(samples)
        signoff = evaluate_signoff(
            agg,
            hard_budget={"enabled": True, "max_aggregate_cpu_percent": 5.0},
        )

        artifact: dict[str, Any] = {
            "version": 1,
            "profile": "hub_only",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:05:00Z",
            "environment": {
                "hostname": "test",
                "platform": "test",
                "python_version": "3.11",
                "car_version": "dev",
                "car_git_ref": None,
                "cpu_count": 8,
                "total_memory_gb": 16.0,
            },
            "aggregate_metrics": agg,
            "per_process_metrics": [],
            "health_probe_summary": {
                "total_probes": 10,
                "successful_probes": 10,
                "failed_probes": 0,
                "last_probe_status": 200,
            },
            "signoff": signoff,
        }

        for key in (
            "version",
            "profile",
            "started_at",
            "finished_at",
            "environment",
            "aggregate_metrics",
            "per_process_metrics",
            "health_probe_summary",
            "signoff",
        ):
            assert key in artifact, f"artifact missing key: {key}"

        for key in (
            "car_owned_cpu_mean_percent",
            "car_owned_cpu_max_percent",
            "car_owned_cpu_p95_percent",
            "car_owned_cpu_samples",
            "car_owned_memory_mean_mb",
            "car_owned_memory_max_mb",
        ):
            assert (
                key in artifact["aggregate_metrics"]
            ), f"aggregate_metrics missing key: {key}"

        for key in (
            "passed",
            "budget_type",
            "actual_aggregate_cpu_percent",
            "message",
        ):
            assert key in artifact["signoff"], f"signoff missing key: {key}"

    def test_artifact_is_valid_json(self, tmp_path: Path):
        from codex_autorunner.core.diagnostics.cpu_sampler import (
            CpuSample,
            aggregate_samples,
            evaluate_signoff,
        )

        samples = [CpuSample(aggregate_cpu_percent=2.0, aggregate_rss_mb=40.0)]
        agg = aggregate_samples(samples)
        signoff = evaluate_signoff(agg)
        artifact = {
            "version": 1,
            "profile": "test",
            "aggregate_metrics": agg,
            "signoff": signoff,
        }
        text = json.dumps(artifact, indent=2, sort_keys=True)
        parsed = json.loads(text)
        assert parsed["profile"] == "test"


class TestIdleCpuSoakWriteArtifacts:
    def test_writes_latest_and_history(self, tmp_path: Path):
        artifact = {
            "version": 1,
            "profile": "hub_only",
            "started_at": "20260101T000000Z",
            "aggregate_metrics": {"car_owned_cpu_mean_percent": 1.0},
            "signoff": {"passed": True},
        }
        artifact_dir = tmp_path / "diag" / "idle-cpu"

        from codex_autorunner.core.utils import atomic_write

        output_root = artifact_dir
        output_root.mkdir(parents=True, exist_ok=True)
        latest_path = output_root / "latest.json"
        atomic_write(latest_path, json.dumps(artifact, indent=2, sort_keys=True) + "\n")

        history_dir = output_root / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_path = history_dir / "20260101T000000Z-hub_only.json"
        atomic_write(
            history_path, json.dumps(artifact, indent=2, sort_keys=True) + "\n"
        )

        assert latest_path.exists()
        assert history_path.exists()
        loaded = json.loads(latest_path.read_text(encoding="utf-8"))
        assert loaded["profile"] == "hub_only"
        loaded_hist = json.loads(history_path.read_text(encoding="utf-8"))
        assert loaded_hist["profile"] == "hub_only"


class TestIdleCpuSoakProfileParsing:
    def test_loads_hub_only_profile(self):
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        profiles_path = _REPO_ROOT / "scripts" / "idle_cpu_profiles.yml"
        data = yaml.safe_load(profiles_path.read_text(encoding="utf-8"))
        profile = data["profiles"]["hub_only"]
        assert profile["hard_budget"]["enabled"] is True
        assert "car hub serve" in profile["services"][0]["command"]

    def test_loads_all_profiles(self):
        profiles_path = _REPO_ROOT / "scripts" / "idle_cpu_profiles.yml"
        data = yaml.safe_load(profiles_path.read_text(encoding="utf-8"))
        profiles = data["profiles"]
        for name in (
            "hub_only",
            "hub_plus_discord",
            "hub_plus_telegram",
            "hub_with_idle_runtime",
        ):
            assert name in profiles
            assert "services" in profiles[name]
            assert "hard_budget" in profiles[name]


class TestIdleCpuSoakProcessManagement:
    def _proc_is_zombie(self, pid: int) -> bool:
        path = Path("/proc") / str(pid) / "stat"
        try:
            fields = path.read_text(encoding="utf-8").split()
        except OSError:
            return False
        return len(fields) > 2 and fields[2] == "Z"

    def _assert_pid_gone(self, pid: int) -> None:
        # Watchdog exits quickly but may remain as a zombie until init reaps it;
        # os.kill(pid, 0) still succeeds for zombies on Linux.
        for _ in range(150):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if self._proc_is_zombie(pid):
                return
            time.sleep(0.1)
        raise AssertionError(f"process {pid} still running")

    def test_start_service_uses_new_session(self, monkeypatch, tmp_path: Path):
        seen: dict[str, Any] = {}

        class FakePopen:
            pid = 4242

            def __init__(self, cmd, **kwargs):
                seen["cmd"] = cmd
                seen.update(kwargs)

        monkeypatch.setattr(soak_module.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(soak_module.os, "getpid", lambda: 3131)

        proc = soak_module._start_service(  # noqa: SLF001
            {
                "alias": "hub",
                "command": ["python", "-m", "codex_autorunner.cli", "hub", "serve"],
            },
            tmp_path / "hub",
            logging.getLogger("test_idle_cpu_soak"),
        )

        assert proc.pid == 4242
        assert seen["start_new_session"] is True
        assert "preexec_fn" not in seen
        assert seen["cmd"][:4] == [
            sys.executable,
            "-c",
            soak_module._SERVICE_WATCHDOG_CODE,  # noqa: SLF001
            "3131",
        ]
        assert seen["cmd"][-2:] == ["--path", str(tmp_path / "hub")]

    def test_collect_service_pids_excludes_watchdog_pid(self, monkeypatch):
        """Session scan includes the watchdog; measurement must omit its RSS/CPU."""

        class FakeProc:
            pid = 1000

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["ps", "-A", "-o", "pid=", "-o", "sid="]:
                # Watchdog is session leader (sid == watchdog pid); child shares sid.
                return subprocess.CompletedProcess(cmd, 0, "1000 1000\n1001 1000\n", "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(soak_module.subprocess, "run", fake_run)
        monkeypatch.setattr(soak_module.os, "getsid", lambda _pid: 1000)

        pids = soak_module._collect_service_pids(  # noqa: SLF001
            [FakeProc()], logging.getLogger("test")
        )
        assert sorted(pids) == [1001]

    def test_write_and_delete_service_record(self, monkeypatch, tmp_path: Path):
        class FakeProc:
            pid = 5151

            def poll(self):
                return 0

        monkeypatch.setattr(soak_module, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(soak_module.os, "getpgid", lambda _pid: 6161)
        monkeypatch.setattr(soak_module.os, "getpid", lambda: 7171)

        proc = FakeProc()
        logger = logging.getLogger("test_idle_cpu_soak")
        soak_module._write_service_record(  # noqa: SLF001
            proc,
            {
                "alias": "hub",
                "command": ["python", "-m", "codex_autorunner.cli", "hub", "serve"],
            },
            "hub_only",
            tmp_path / "idle-cpu-soak-abcd" / "hub",
            "http://127.0.0.1:12345",
            logger,
        )

        record = (
            tmp_path / ".codex-autorunner" / "processes" / "idle_cpu_soak" / "5151.json"
        )
        assert record.exists()
        payload = json.loads(record.read_text(encoding="utf-8"))
        assert payload["pid"] == 5151
        assert payload["pgid"] == 6161
        assert payload["owner_pid"] == 7171
        assert payload["metadata"]["profile"] == "hub_only"

        soak_module._stop_service(proc, logger)  # noqa: SLF001

        assert record.exists() is False

    def test_service_watchdog_exits_when_harness_parent_exits(self, tmp_path: Path):
        helper = f"""
import logging
import pathlib
import sys

sys.path.insert(0, {str(_REPO_ROOT / "scripts")!r})
import idle_cpu_soak as soak

proc = soak._start_service(
    {{
        "alias": "sleeper",
        "command": [
            sys.executable,
            "-c",
            "import time; time.sleep(300)",
        ],
    }},
    pathlib.Path({str(tmp_path / "hub")!r}),
    logging.getLogger("test_idle_cpu_soak.helper"),
)
print(proc.pid, flush=True)
"""
        result = subprocess.run(
            [sys.executable, "-c", helper],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        wrapper_pid = int(result.stdout.strip().splitlines()[-1])

        self._assert_pid_gone(wrapper_pid)


class TestIdleCpuSoakSignoffLogic:
    def test_hard_budget_pass_at_threshold(self):
        from codex_autorunner.core.diagnostics.cpu_sampler import evaluate_signoff

        agg = {"car_owned_cpu_mean_percent": 5.0}
        result = evaluate_signoff(
            agg,
            hard_budget={"enabled": True, "max_aggregate_cpu_percent": 5.0},
        )
        assert result["passed"] is True

    def test_hard_budget_fail_above_threshold(self):
        from codex_autorunner.core.diagnostics.cpu_sampler import evaluate_signoff

        agg = {"car_owned_cpu_mean_percent": 5.001}
        result = evaluate_signoff(
            agg,
            hard_budget={"enabled": True, "max_aggregate_cpu_percent": 5.0},
        )
        assert result["passed"] is False

    def test_comparison_budget_is_not_hard_gate(self):
        from codex_autorunner.core.diagnostics.cpu_sampler import evaluate_signoff

        agg = {"car_owned_cpu_mean_percent": 99.0}
        result = evaluate_signoff(
            agg,
            hard_budget={"enabled": False},
            comparison_budget={"max_aggregate_cpu_percent": 8.0},
        )
        assert result["budget_type"] == "comparison"
        assert result["passed"] is False


class TestIdleCpuSoakIntegration:
    @pytest.mark.integration
    def test_smoke_soak_against_disposable_root(self, tmp_path: Path):
        if not _SOAK_SCRIPT.exists():
            pytest.skip("soak script not found")

        artifact_dir = tmp_path / "artifacts"
        result = subprocess.run(
            [
                sys.executable,
                str(_SOAK_SCRIPT),
                "--profile",
                "hub_only",
                "--artifact-dir",
                str(artifact_dir),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(_REPO_ROOT),
        )

        latest = artifact_dir / "latest.json"
        assert (
            latest.exists()
        ), f"latest.json not found. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

        artifact = json.loads(latest.read_text(encoding="utf-8"))
        assert artifact["profile"] == "hub_only"
        assert "aggregate_metrics" in artifact
        assert "signoff" in artifact
        assert artifact["version"] == 1

        agg = artifact["aggregate_metrics"]
        assert agg["car_owned_cpu_samples"] > 0
        assert agg["car_owned_cpu_mean_percent"] >= 0.0

        history_dir = artifact_dir / "history"
        assert history_dir.exists()
        history_files = list(history_dir.glob("*.json"))
        assert len(history_files) >= 1
