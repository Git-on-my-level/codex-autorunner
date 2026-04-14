from codex_autorunner.integrations.chat.status_diagnostics import (
    StatusBlockContext,
    build_status_block_lines,
    extract_rate_limits,
    format_process_monitor_lines,
)


class TestBuildStatusBlockLines:
    def test_renders_shared_runtime_fields_in_order(self) -> None:
        context = StatusBlockContext(
            agent="codex",
            resume="supported",
            model="gpt-5.4",
            effort="high",
            approval_mode="yolo",
            approval_policy="never",
            sandbox_policy={"type": "dangerFullAccess", "networkAccess": True},
            rate_limits={"primary": {"used_percent": 5, "window_minutes": 300}},
            thread_id="thread-123",
            turn_id="turn-456",
            extra_lines=("Summary: ready", " ", ""),
        )

        lines = build_status_block_lines(context)

        assert lines == [
            "Agent: codex",
            "Resume: supported",
            "Model: gpt-5.4",
            "Effort: high",
            "Approval mode: yolo",
            "Approval policy: never",
            "Sandbox policy: dangerFullAccess, network=True",
            "Limits: [5h: 5%]",
            "Active thread: thread-123",
            "Active turn: turn-456",
            "Summary: ready",
        ]

    def test_skips_blank_shared_values(self) -> None:
        context = StatusBlockContext(
            agent="  ",
            resume=None,
            model="gpt-5.4",
            effort="",
            sandbox_policy=None,
            rate_limits=None,
            thread_id=None,
            turn_id=None,
            extra_lines=("  ", "Notes: included"),
        )

        lines = build_status_block_lines(context)

        assert lines == [
            "Model: gpt-5.4",
            "Sandbox policy: default",
            "Notes: included",
        ]


class TestExtractRateLimits:
    def test_prefers_nested_rate_limit_mappings(self) -> None:
        assert extract_rate_limits(
            {"rateLimits": {"primary": {"used_percent": 5}}}
        ) == {"primary": {"used_percent": 5}}
        assert extract_rate_limits(
            {"rate_limits": {"primary": {"used_percent": 6}}}
        ) == {"primary": {"used_percent": 6}}
        assert extract_rate_limits({"limits": {"primary": {"used_percent": 7}}}) == {
            "primary": {"used_percent": 7}
        }

    def test_accepts_flat_primary_secondary_payloads(self) -> None:
        payload = {"primary": {"used_percent": 5}, "secondary": {"used_percent": 8}}

        assert extract_rate_limits(payload) == payload


class TestFormatProcessMonitorLines:
    def test_renders_history_with_tp95_when_requested(self) -> None:
        lines = format_process_monitor_lines(
            {
                "status": "warning",
                "sample_count": 42,
                "cadence_seconds": 120,
                "window_seconds": 3 * 60 * 60,
                "metrics": {
                    "car_services": {
                        "current": 6,
                        "average": 4.0,
                        "p95": 5,
                        "peak": 6,
                        "abnormal": False,
                        "reason": None,
                    },
                    "managed_runtimes": {
                        "current": 16,
                        "average": 5.25,
                        "p95": 12,
                        "peak": 16,
                        "abnormal": True,
                        "reason": "high",
                    },
                    "opencode": {
                        "current": 18,
                        "average": 7.25,
                        "p95": 14,
                        "peak": 18,
                        "abnormal": True,
                        "reason": "high",
                    },
                    "codex_app_server": {
                        "current": 4,
                        "average": 2.0,
                        "p95": 3,
                        "peak": 4,
                        "abnormal": False,
                        "reason": None,
                    },
                    "total": {
                        "current": 22,
                        "average": 9.25,
                        "p95": 17,
                        "peak": 22,
                        "abnormal": True,
                        "reason": "high",
                    },
                },
                "latest": {},
            },
            include_history=True,
        )

        assert (
            lines[0] == "Process monitor: warning (window=3h samples=42 cadence=120s)"
        )
        assert "CAR services: 6 (avg 4.0, tp95 5, peak 6)" in lines
        assert "Managed runtimes: 16 (avg 5.2, tp95 12, peak 16) high" in lines
        assert "OpenCode: 18 (avg 7.2, tp95 14, peak 18) high" in lines
        assert "Codex app-server: 4 (avg 2.0, tp95 3, peak 4)" in lines
        assert "Total: 22 (avg 9.2, tp95 17, peak 22) high" in lines
