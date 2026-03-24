from codex_autorunner.integrations.chat.status_diagnostics import (
    StatusBlockContext,
    build_status_block_lines,
    extract_rate_limits,
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
