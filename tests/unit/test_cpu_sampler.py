from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from codex_autorunner.core.diagnostics.cpu_sampler import (
    CpuSample,
    aggregate_samples,
    collect_cpu_sample,
    compute_per_process_aggregates,
    evaluate_signoff,
    sample_cpu_for_pids,
)
from codex_autorunner.core.diagnostics.process_snapshot import ProcessCategory


class TestSampleCpuForPids:
    def test_empty_pids_returns_empty(self):
        assert sample_cpu_for_pids([]) == {}

    def test_parses_valid_ps_output(self):
        def _mock_run(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "  123  1.5  10240\n  456  0.3  5120\n"
            return proc

        import subprocess

        original = subprocess.run
        subprocess.run = _mock_run
        try:
            result = sample_cpu_for_pids([123, 456])
        finally:
            subprocess.run = original

        assert 123 in result
        assert 456 in result
        assert result[123] == (1.5, 10.0)
        assert result[456] == (0.3, 5.0)

    def test_malformed_ps_line_skipped(self):
        def _mock_run(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "  bad  xyz  abc\n  456  0.3  5120\n"
            return proc

        import subprocess

        original = subprocess.run
        subprocess.run = _mock_run
        try:
            result = sample_cpu_for_pids([456])
        finally:
            subprocess.run = original

        assert 456 in result
        assert 123 not in result

    def test_ps_failure_returns_empty(self):
        def _mock_run(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 1
            proc.stdout = ""
            return proc

        import subprocess

        original = subprocess.run
        subprocess.run = _mock_run
        try:
            result = sample_cpu_for_pids([123])
        finally:
            subprocess.run = original

        assert result == {}

    def test_oserror_returns_empty(self):
        import subprocess

        original = subprocess.run

        def _raise(*args, **kwargs):
            raise OSError("no ps")

        subprocess.run = _raise
        try:
            result = sample_cpu_for_pids([123])
        finally:
            subprocess.run = original

        assert result == {}

    def test_incomplete_line_skipped(self):
        def _mock_run(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "  123  1.5\n"
            return proc

        import subprocess

        original = subprocess.run
        subprocess.run = _mock_run
        try:
            result = sample_cpu_for_pids([123])
        finally:
            subprocess.run = original

        assert result == {}

    def test_empty_output_returns_empty(self):
        def _mock_run(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "\n\n"
            return proc

        import subprocess

        original = subprocess.run
        subprocess.run = _mock_run
        try:
            result = sample_cpu_for_pids([123])
        finally:
            subprocess.run = original

        assert result == {}


class TestCollectCpuSample:
    def test_no_car_processes(self):
        def _mock_ps():
            return ""

        def _mock_cpu(pids):
            return {}

        sample = collect_cpu_sample(
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert sample.aggregate_cpu_percent == 0.0
        assert sample.per_process == []

    def test_with_car_service_processes(self):
        ps_output = (
            "  100  1  1  50000  00:01:00  codex-autorunner hub serve\n"
            "  200  1  1  10000  00:00:30  something else\n"
        )

        def _mock_ps():
            return ps_output

        def _mock_cpu(pids):
            result = {}
            if 100 in pids:
                result[100] = (2.5, 48.828)
            return result

        sample = collect_cpu_sample(
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert sample.car_service_cpu_percent == 2.5
        assert sample.aggregate_cpu_percent == 2.5
        assert len(sample.per_process) == 1
        assert sample.per_process[0]["pid"] == 100
        assert sample.per_process[0]["category"] == "car_service"

    def test_multiple_categories(self):
        ps_output = (
            "  100  1  1  50000  00:01:00  codex-autorunner hub serve\n"
            "  200  1  1  20000  00:00:30  opencode serve\n"
            "  300  1  1  15000  00:00:15  codex app-server\n"
        )

        def _mock_ps():
            return ps_output

        def _mock_cpu(pids):
            return {
                100: (1.0, 48.828),
                200: (0.5, 19.531),
                300: (0.3, 14.648),
            }

        sample = collect_cpu_sample(
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert sample.car_service_cpu_percent == 1.0
        assert sample.opencode_cpu_percent == 0.5
        assert sample.app_server_cpu_percent == 0.3
        assert sample.aggregate_cpu_percent == pytest.approx(1.8, abs=0.01)
        assert len(sample.per_process) == 3

    def test_filtered_categories(self):
        ps_output = (
            "  100  1  1  50000  00:01:00  codex-autorunner hub serve\n"
            "  200  1  1  20000  00:00:30  opencode serve\n"
        )

        def _mock_ps():
            return ps_output

        def _mock_cpu(pids):
            return {100: (1.0, 48.828)}

        sample = collect_cpu_sample(
            owned_categories=[ProcessCategory.CAR_SERVICE],
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert sample.car_service_cpu_percent == 1.0
        assert sample.opencode_cpu_percent == 0.0
        assert sample.aggregate_cpu_percent == 1.0
        assert len(sample.per_process) == 1


class TestAggregateSamples:
    def test_empty_samples(self):
        result = aggregate_samples([])
        assert result["car_owned_cpu_mean_percent"] == 0.0
        assert result["car_owned_cpu_max_percent"] == 0.0
        assert result["car_owned_cpu_p95_percent"] == 0.0
        assert result["car_owned_cpu_samples"] == 0

    def test_single_sample(self):
        samples = [CpuSample(aggregate_cpu_percent=3.5, aggregate_rss_mb=50.0)]
        result = aggregate_samples(samples)
        assert result["car_owned_cpu_mean_percent"] == 3.5
        assert result["car_owned_cpu_max_percent"] == 3.5
        assert result["car_owned_cpu_p95_percent"] == 3.5
        assert result["car_owned_cpu_samples"] == 1
        assert result["car_owned_memory_mean_mb"] == 50.0

    def test_multiple_samples(self):
        samples = [
            CpuSample(aggregate_cpu_percent=1.0, aggregate_rss_mb=40.0),
            CpuSample(aggregate_cpu_percent=2.0, aggregate_rss_mb=50.0),
            CpuSample(aggregate_cpu_percent=3.0, aggregate_rss_mb=60.0),
            CpuSample(aggregate_cpu_percent=4.0, aggregate_rss_mb=70.0),
            CpuSample(aggregate_cpu_percent=5.0, aggregate_rss_mb=80.0),
        ]
        result = aggregate_samples(samples)
        assert result["car_owned_cpu_mean_percent"] == 3.0
        assert result["car_owned_cpu_max_percent"] == 5.0
        assert result["car_owned_cpu_samples"] == 5
        assert result["car_owned_memory_mean_mb"] == 60.0
        assert result["car_owned_memory_max_mb"] == 80.0

    def test_p95_computation(self):
        samples = [CpuSample(aggregate_cpu_percent=float(i)) for i in range(1, 21)]
        result = aggregate_samples(samples)
        assert result["car_owned_cpu_mean_percent"] == 10.5
        assert result["car_owned_cpu_max_percent"] == 20.0
        assert result["car_owned_cpu_p95_percent"] == 19.0


class TestComputePerProcessAggregates:
    def test_empty_samples(self):
        assert compute_per_process_aggregates([]) == []

    def test_single_process_multiple_samples(self):
        samples = [
            CpuSample(
                per_process=[
                    {
                        "pid": 100,
                        "command": "car hub",
                        "category": "car_service",
                        "cpu_percent": 1.0,
                        "rss_mb": 50.0,
                    }
                ]
            ),
            CpuSample(
                per_process=[
                    {
                        "pid": 100,
                        "command": "car hub",
                        "category": "car_service",
                        "cpu_percent": 3.0,
                        "rss_mb": 60.0,
                    }
                ]
            ),
        ]
        result = compute_per_process_aggregates(samples)
        assert len(result) == 1
        assert result[0]["cpu_mean_percent"] == 2.0
        assert result[0]["cpu_max_percent"] == 3.0
        assert result[0]["memory_mean_mb"] == 55.0
        assert result[0]["memory_max_mb"] == 60.0

    def test_multiple_processes(self):
        samples = [
            CpuSample(
                per_process=[
                    {
                        "pid": 100,
                        "command": "car hub",
                        "category": "car_service",
                        "cpu_percent": 1.0,
                        "rss_mb": 50.0,
                    },
                    {
                        "pid": 200,
                        "command": "opencode",
                        "category": "opencode",
                        "cpu_percent": 0.5,
                        "rss_mb": 30.0,
                    },
                ]
            ),
        ]
        result = compute_per_process_aggregates(samples)
        assert len(result) == 2


class TestEvaluateSignoff:
    def test_hard_budget_pass(self):
        agg = {"car_owned_cpu_mean_percent": 3.0}
        result = evaluate_signoff(
            agg,
            hard_budget={"enabled": True, "max_aggregate_cpu_percent": 5.0},
        )
        assert result["passed"] is True
        assert result["budget_type"] == "hard"
        assert result["budget_threshold_percent"] == 5.0
        assert "PASS" in result["message"]

    def test_hard_budget_fail(self):
        agg = {"car_owned_cpu_mean_percent": 7.5}
        result = evaluate_signoff(
            agg,
            hard_budget={"enabled": True, "max_aggregate_cpu_percent": 5.0},
        )
        assert result["passed"] is False
        assert result["budget_type"] == "hard"
        assert "FAIL" in result["message"]

    def test_comparison_budget_pass(self):
        agg = {"car_owned_cpu_mean_percent": 5.0}
        result = evaluate_signoff(
            agg,
            comparison_budget={"max_aggregate_cpu_percent": 8.0},
        )
        assert result["passed"] is True
        assert result["budget_type"] == "comparison"

    def test_comparison_budget_fail(self):
        agg = {"car_owned_cpu_mean_percent": 10.0}
        result = evaluate_signoff(
            agg,
            comparison_budget={"max_aggregate_cpu_percent": 8.0},
        )
        assert result["passed"] is False
        assert result["budget_type"] == "comparison"

    def test_no_budget(self):
        agg = {"car_owned_cpu_mean_percent": 42.0}
        result = evaluate_signoff(agg)
        assert result["passed"] is True
        assert result["budget_type"] == "none"
        assert result["budget_threshold_percent"] is None

    def test_hard_budget_disabled(self):
        agg = {"car_owned_cpu_mean_percent": 99.0}
        result = evaluate_signoff(
            agg,
            hard_budget={"enabled": False},
        )
        assert result["passed"] is True
        assert result["budget_type"] == "none"

    def test_missing_cpu_key_defaults_zero(self):
        agg: dict[str, Any] = {}
        result = evaluate_signoff(
            agg,
            hard_budget={"enabled": True, "max_aggregate_cpu_percent": 5.0},
        )
        assert result["passed"] is True
        assert result["actual_aggregate_cpu_percent"] == 0.0


class TestCpuSample:
    def test_defaults(self):
        s = CpuSample()
        assert s.aggregate_cpu_percent == 0.0
        assert s.aggregate_rss_mb == 0.0
        assert s.per_process == []

    def test_values(self):
        s = CpuSample(
            car_service_cpu_percent=1.5,
            car_service_rss_mb=40.0,
            aggregate_cpu_percent=1.5,
            aggregate_rss_mb=40.0,
        )
        assert s.car_service_cpu_percent == 1.5
        assert s.aggregate_rss_mb == 40.0


class TestMalformedPsOutput:
    def test_garbage_lines_skipped(self):
        ps_output = "not a real line\n  also bad\n"

        def _mock_ps():
            return ps_output

        def _mock_cpu(pids):
            return {}

        sample = collect_cpu_sample(
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert sample.aggregate_cpu_percent == 0.0

    def test_partial_columns_skipped(self):
        ps_output = "  100  1\n"

        def _mock_ps():
            return ps_output

        def _mock_cpu(pids):
            return {}

        sample = collect_cpu_sample(
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert sample.aggregate_cpu_percent == 0.0

    def test_non_numeric_pid_skipped(self):
        ps_output = "  abc  1  1  50000  00:01:00  codex-autorunner hub serve\n"

        def _mock_ps():
            return ps_output

        def _mock_cpu(pids):
            return {}

        sample = collect_cpu_sample(
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert sample.aggregate_cpu_percent == 0.0

    def test_negative_rss_handled(self):
        ps_output = "  100  1  1  -500  00:01:00  codex-autorunner hub serve\n"

        def _mock_ps():
            return ps_output

        def _mock_cpu(pids):
            return {100: (1.0, 0.0)}

        sample = collect_cpu_sample(
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert len(sample.per_process) == 1

    def test_very_long_command_truncated_by_split(self):
        ps_output = (
            "  100  1  1  50000  00:01:00  codex-autorunner hub serve "
            + "x" * 500
            + "\n"
        )

        def _mock_ps():
            return ps_output

        def _mock_cpu(pids):
            return {100: (1.0, 48.828)}

        sample = collect_cpu_sample(
            ps_output_getter=_mock_ps,
            pid_cpu_getter=_mock_cpu,
        )
        assert sample.aggregate_cpu_percent == 1.0
