from __future__ import annotations

import time
from pathlib import Path

import pytest

from codex_autorunner.core.pma_audit import PmaActionType, PmaAuditEntry, PmaAuditLog
from codex_autorunner.core.pma_safety import (
    PmaSafetyChecker,
    PmaSafetyConfig,
    SafetyCheckResult,
)


@pytest.fixture
def hub_root(tmp_path: Path) -> Path:
    return tmp_path / "hub"


class TestPmaAuditEntry:
    def test_fingerprint_generated(self) -> None:
        entry = PmaAuditEntry(
            action_type=PmaActionType.CHAT_STARTED,
            agent="codex",
            details={"message": "test"},
        )
        assert entry.entry_id
        assert entry.fingerprint
        assert len(entry.fingerprint) == 16


class TestPmaAuditLog:
    def test_append_creates_file(self, tmp_path: Path) -> None:
        log = PmaAuditLog(tmp_path)
        entry = PmaAuditEntry(
            action_type=PmaActionType.CHAT_STARTED,
            agent="codex",
            details={"message": "test"},
        )
        entry_id = log.append(entry)
        assert entry_id == entry.entry_id
        assert log.path.exists()

    def test_list_recent_returns_limited_entries(self, tmp_path: Path) -> None:
        log = PmaAuditLog(tmp_path)
        for i in range(5):
            entry = PmaAuditEntry(
                action_type=PmaActionType.CHAT_STARTED,
                agent="codex",
                details={"message": f"test-{i}"},
            )
            log.append(entry)
        entries = log.list_recent(limit=3)
        assert len(entries) == 3
        assert entries[0].details["message"] == "test-2"

    def test_count_fingerprint_returns_count(self, tmp_path: Path) -> None:
        log = PmaAuditLog(tmp_path)
        entry = PmaAuditEntry(
            action_type=PmaActionType.CHAT_STARTED,
            agent="codex",
            details={"message": "test"},
        )
        fingerprint = entry.fingerprint
        log.append(entry)
        log.append(entry)
        count = log.count_fingerprint(fingerprint)
        assert count == 2

    def test_prune_old_removes_excess_entries(self, tmp_path: Path) -> None:
        log = PmaAuditLog(tmp_path)
        for i in range(10):
            entry = PmaAuditEntry(
                action_type=PmaActionType.CHAT_STARTED,
                agent="codex",
                details={"message": f"test-{i}"},
            )
            log.append(entry)
        pruned = log.prune_old(keep_last=5)
        assert pruned == 5
        entries = log.list_recent(limit=100)
        assert len(entries) == 5


@pytest.fixture
def config() -> PmaSafetyConfig:
    return PmaSafetyConfig(
        dedup_window_seconds=60,
        max_duplicate_actions=3,
        rate_limit_window_seconds=10,
        max_actions_per_window=5,
        circuit_breaker_threshold=3,
        circuit_breaker_cooldown_seconds=30,
    )


@pytest.fixture
def checker(hub_root: Path, config: PmaSafetyConfig) -> PmaSafetyChecker:
    return PmaSafetyChecker(hub_root, config=config)


class TestSafetyCheckResult:
    def test_defaults(self) -> None:
        result = SafetyCheckResult(allowed=True)
        assert result.allowed is True
        assert result.reason is None
        assert result.details == {}

    def test_with_reason_and_details(self) -> None:
        result = SafetyCheckResult(
            allowed=False,
            reason="test_reason",
            details={"key": "value"},
        )
        assert result.allowed is False
        assert result.reason == "test_reason"
        assert result.details == {"key": "value"}


class TestPmaSafetyConfig:
    def test_default_values(self) -> None:
        config = PmaSafetyConfig()
        assert config.dedup_window_seconds == 300
        assert config.max_duplicate_actions == 3
        assert config.rate_limit_window_seconds == 60
        assert config.max_actions_per_window == 20
        assert config.circuit_breaker_threshold == 5
        assert config.circuit_breaker_cooldown_seconds == 600
        assert config.enable_dedup is True
        assert config.enable_rate_limit is True
        assert config.enable_circuit_breaker is True

    def test_custom_values(self) -> None:
        config = PmaSafetyConfig(
            dedup_window_seconds=100,
            max_duplicate_actions=5,
            enable_dedup=False,
        )
        assert config.dedup_window_seconds == 100
        assert config.max_duplicate_actions == 5
        assert config.enable_dedup is False


class TestDuplicateDetection:
    def test_compute_fingerprint_consistency(self, checker: PmaSafetyChecker) -> None:
        fp1 = checker._compute_chat_fingerprint("agent1", "hello world")
        fp2 = checker._compute_chat_fingerprint("agent1", "hello world")
        assert fp1 == fp2

    def test_compute_fingerprint_different_agent(
        self, checker: PmaSafetyChecker
    ) -> None:
        fp1 = checker._compute_chat_fingerprint("agent1", "hello")
        fp2 = checker._compute_chat_fingerprint("agent2", "hello")
        assert fp1 != fp2

    def test_compute_fingerprint_different_message(
        self, checker: PmaSafetyChecker
    ) -> None:
        fp1 = checker._compute_chat_fingerprint("agent1", "hello")
        fp2 = checker._compute_chat_fingerprint("agent1", "goodbye")
        assert fp1 != fp2

    def test_message_truncated_in_fingerprint(self, checker: PmaSafetyChecker) -> None:
        long_msg = "x" * 500
        fp1 = checker._compute_chat_fingerprint("agent1", long_msg)
        fp2 = checker._compute_chat_fingerprint("agent1", "x" * 200)
        assert fp1 == fp2

    def test_duplicate_detection_blocks_after_threshold(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_rate_limit = False
        config.enable_circuit_breaker = False
        config.max_duplicate_actions = 2
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.max_duplicate_actions):
            checker.record_action(
                PmaActionType.CHAT_STARTED,
                agent="agent1",
                details={"message_truncated": "test message"[:200]},
            )

        result = checker.check_chat_start("agent1", "test message")
        assert result.allowed is False
        assert result.reason == "duplicate_action"
        assert "fingerprint" in result.details
        assert result.details["max_allowed"] == config.max_duplicate_actions

    def test_duplicate_detection_allows_different_messages(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_rate_limit = False
        config.enable_circuit_breaker = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for i in range(config.max_duplicate_actions + 1):
            result = checker.check_chat_start("agent1", f"unique message {i}")
            assert result.allowed is True

    def test_duplicate_detection_window_expiry(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.dedup_window_seconds = 0.1
        config.enable_rate_limit = False
        config.enable_circuit_breaker = False
        config.max_duplicate_actions = 2
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.max_duplicate_actions):
            checker.record_action(
                PmaActionType.CHAT_STARTED,
                agent="agent1",
                details={"message_truncated": "test"[:200]},
            )

        result = checker.check_chat_start("agent1", "test")
        assert result.allowed is False
        assert result.reason == "duplicate_action"

        time.sleep(0.15)

        result = checker.check_chat_start("agent1", "test")
        assert result.allowed is True

    def test_duplicate_detection_disabled(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_dedup = False
        config.enable_rate_limit = False
        config.enable_circuit_breaker = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.max_duplicate_actions + 5):
            checker.record_action(
                PmaActionType.CHAT_STARTED,
                agent="agent1",
                details={"message_truncated": "same"[:200]},
            )

        result = checker.check_chat_start("agent1", "same")
        assert result.allowed is True


class TestRateLimiting:
    def test_rate_limit_allows_within_limit(self, checker: PmaSafetyChecker) -> None:
        for i in range(checker._config.max_actions_per_window):
            result = checker.check_chat_start("agent1", f"msg{i}")
            assert result.allowed is True

    def test_rate_limit_blocks_over_limit(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_dedup = False
        config.enable_circuit_breaker = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for i in range(config.max_actions_per_window):
            checker.check_chat_start("agent1", f"msg{i}")

        result = checker.check_chat_start("agent1", "over limit")
        assert result.allowed is False
        assert result.reason == "rate_limit_exceeded"
        assert result.details["agent"] == "agent1"
        assert result.details["max_allowed"] == config.max_actions_per_window

    def test_rate_limit_per_agent_isolation(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_dedup = False
        config.enable_circuit_breaker = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for i in range(config.max_actions_per_window):
            checker.check_chat_start("agent1", f"msg{i}")

        result1 = checker.check_chat_start("agent1", "over")
        assert result1.allowed is False

        result2 = checker.check_chat_start("agent2", "msg")
        assert result2.allowed is True

    @pytest.mark.slow
    def test_rate_limit_window_sliding(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.rate_limit_window_seconds = 0.8
        config.max_actions_per_window = 2
        config.enable_dedup = False
        config.enable_circuit_breaker = False
        checker = PmaSafetyChecker(hub_root, config=config)

        checker.check_chat_start("agent1", "msg1")

        time.sleep(0.6)
        checker.check_chat_start("agent1", "msg2")

        result = checker.check_chat_start("agent1", "msg3")
        assert result.allowed is False

        time.sleep(0.6)

        result = checker.check_chat_start("agent1", "msg4")
        assert result.allowed is True

    def test_rate_limit_disabled(self, hub_root: Path, config: PmaSafetyConfig) -> None:
        config.enable_rate_limit = False
        config.enable_dedup = False
        config.enable_circuit_breaker = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for i in range(config.max_actions_per_window * 3):
            result = checker.check_chat_start("agent1", f"msg{i}")
            assert result.allowed is True


class TestCircuitBreaker:
    def test_circuit_breaker_inactive_by_default(
        self, checker: PmaSafetyChecker
    ) -> None:
        assert checker._is_circuit_breaker_active() is False

    def test_circuit_breaker_activates_on_threshold(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_dedup = False
        config.enable_rate_limit = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold):
            checker.record_chat_result("agent1", "error")

        assert checker._is_circuit_breaker_active() is True

    def test_circuit_breaker_blocks_chat_start(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_dedup = False
        config.enable_rate_limit = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold):
            checker.record_chat_result("agent1", "error")

        result = checker.check_chat_start("agent1", "test")
        assert result.allowed is False
        assert result.reason == "circuit_breaker_active"
        assert "cooldown_remaining_seconds" in result.details

    def test_circuit_breaker_blocks_reactive_turn(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold):
            checker.record_reactive_result(status="error")

        result = checker.check_reactive_turn()
        assert result.allowed is False
        assert result.reason == "circuit_breaker_active"

    def test_circuit_breaker_resets_after_cooldown(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.circuit_breaker_cooldown_seconds = 0.1
        config.enable_dedup = False
        config.enable_rate_limit = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold):
            checker.record_chat_result("agent1", "error")

        assert checker._is_circuit_breaker_active() is True

        time.sleep(0.15)

        result = checker.check_chat_start("agent1", "test")
        assert result.allowed is True

    def test_circuit_breaker_reset_clears_failure_counts(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.circuit_breaker_cooldown_seconds = 0.1
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold):
            checker.record_chat_result("agent1", "error")

        assert checker._failure_counts["chat:agent1"] > 0
        assert checker._is_circuit_breaker_active() is True

        time.sleep(0.15)
        checker._is_circuit_breaker_active()

        assert checker._failure_counts == {}

    def test_circuit_breaker_disabled(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_circuit_breaker = False
        config.enable_dedup = False
        config.enable_rate_limit = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold * 2):
            checker.record_chat_result("agent1", "error")

        assert checker._is_circuit_breaker_active() is False
        result = checker.check_chat_start("agent1", "test")
        assert result.allowed is True

    def test_circuit_breaker_counts_multiple_agents_separately(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.circuit_breaker_threshold = 2
        checker = PmaSafetyChecker(hub_root, config=config)

        checker.record_chat_result("agent1", "error")
        checker.record_chat_result("agent2", "error")

        assert checker._is_circuit_breaker_active() is False

        checker.record_chat_result("agent1", "error")
        assert checker._is_circuit_breaker_active() is True


class TestCheckChatStart:
    def test_allows_when_all_checks_pass(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        checker = PmaSafetyChecker(hub_root, config=config)
        result = checker.check_chat_start("agent1", "hello")
        assert result.allowed is True

    def test_check_order_circuit_breaker_first(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_dedup = False
        config.enable_rate_limit = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold):
            checker.record_chat_result("agent1", "error")

        for _i in range(config.max_duplicate_actions + 1):
            checker.record_action(
                PmaActionType.CHAT_STARTED,
                agent="agent1",
                details={"message_truncated": "dup"[:200]},
            )

        result = checker.check_chat_start("agent1", "dup")
        assert result.allowed is False
        assert result.reason == "circuit_breaker_active"

    def test_check_order_dedup_before_rate_limit(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_circuit_breaker = False
        config.max_duplicate_actions = 1
        checker = PmaSafetyChecker(hub_root, config=config)

        checker.record_action(
            PmaActionType.CHAT_STARTED,
            agent="agent1",
            details={"message_truncated": "dup"[:200]},
        )

        for i in range(config.max_actions_per_window):
            checker.check_chat_start("agent1", f"unique{i}")

        result = checker.check_chat_start("agent1", "dup")
        assert result.allowed is False
        assert result.reason == "duplicate_action"


class TestCheckReactiveTurn:
    def test_allows_when_rate_limit_not_exceeded(
        self, checker: PmaSafetyChecker
    ) -> None:
        for _i in range(checker._config.max_actions_per_window):
            result = checker.check_reactive_turn()
            assert result.allowed is True

    def test_blocks_when_rate_limit_exceeded(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.max_actions_per_window):
            checker.check_reactive_turn()

        result = checker.check_reactive_turn()
        assert result.allowed is False
        assert result.reason == "rate_limit_exceeded"
        assert result.details["key"] == "reactive"

    def test_custom_key_rate_limiting(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.max_actions_per_window):
            checker.check_reactive_turn(key="custom_key")

        result1 = checker.check_reactive_turn(key="custom_key")
        assert result1.allowed is False

        result2 = checker.check_reactive_turn(key="other_key")
        assert result2.allowed is True

    def test_rate_limit_disabled_allows_all(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_rate_limit = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.max_actions_per_window * 3):
            result = checker.check_reactive_turn()
            assert result.allowed is True


class TestRecordChatResult:
    def test_increments_failure_count_on_error(self, checker: PmaSafetyChecker) -> None:
        checker.record_chat_result("agent1", "error")
        assert checker._failure_counts["chat:agent1"] == 1

        checker.record_chat_result("agent1", "failed")
        assert checker._failure_counts["chat:agent1"] == 2

        checker.record_chat_result("agent1", "interrupted")
        assert checker._failure_counts["chat:agent1"] == 3

    def test_resets_failure_count_on_success(self, checker: PmaSafetyChecker) -> None:
        checker.record_chat_result("agent1", "error")
        checker.record_chat_result("agent1", "error")
        assert checker._failure_counts["chat:agent1"] == 2

        checker.record_chat_result("agent1", "ok")
        assert checker._failure_counts["chat:agent1"] == 0

    def test_resets_failure_count_on_other_status(
        self, checker: PmaSafetyChecker
    ) -> None:
        checker.record_chat_result("agent1", "error")
        assert checker._failure_counts["chat:agent1"] == 1

        checker.record_chat_result("agent1", "completed")
        assert checker._failure_counts["chat:agent1"] == 0

    def test_circuit_breaker_disabled_no_activation(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_circuit_breaker = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold * 2):
            checker.record_chat_result("agent1", "error")

        assert checker._is_circuit_breaker_active() is False
        assert checker._failure_counts["chat:agent1"] == 0

    def test_activates_circuit_breaker_at_threshold(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold - 1):
            checker.record_chat_result("agent1", "error")
        assert checker._is_circuit_breaker_active() is False

        checker.record_chat_result("agent1", "error")
        assert checker._is_circuit_breaker_active() is True


class TestRecordReactiveResult:
    def test_increments_failure_count_on_error(self, checker: PmaSafetyChecker) -> None:
        checker.record_reactive_result(status="error")
        assert checker._failure_counts["reactive"] == 1

        checker.record_reactive_result(status="failed")
        assert checker._failure_counts["reactive"] == 2

        checker.record_reactive_result(status="interrupted")
        assert checker._failure_counts["reactive"] == 3

    def test_resets_failure_count_on_success(self, checker: PmaSafetyChecker) -> None:
        checker.record_reactive_result(status="error")
        checker.record_reactive_result(status="error")
        assert checker._failure_counts["reactive"] == 2

        checker.record_reactive_result(status="ok")
        assert checker._failure_counts["reactive"] == 0

    def test_custom_key_failure_counting(self, checker: PmaSafetyChecker) -> None:
        checker.record_reactive_result(status="error", key="custom")
        assert checker._failure_counts["custom"] == 1
        assert checker._failure_counts.get("reactive", 0) == 0

    def test_activates_circuit_breaker_at_threshold(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold):
            checker.record_reactive_result(status="error")

        assert checker._is_circuit_breaker_active() is True

    def test_circuit_breaker_disabled_no_activation(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.enable_circuit_breaker = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold * 2):
            checker.record_reactive_result(status="error")

        assert checker._is_circuit_breaker_active() is False
        assert checker._failure_counts["reactive"] == 0


class TestGetStats:
    def test_returns_circuit_breaker_status(self, checker: PmaSafetyChecker) -> None:
        stats = checker.get_stats()
        assert "circuit_breaker_active" in stats
        assert stats["circuit_breaker_active"] is False
        assert "circuit_breaker_cooldown_remaining" in stats

    def test_returns_failure_counts(self, checker: PmaSafetyChecker) -> None:
        checker.record_chat_result("agent1", "error")
        checker.record_reactive_result(status="error")

        stats = checker.get_stats()
        assert stats["failure_counts"]["chat:agent1"] == 1
        assert stats["failure_counts"]["reactive"] == 1

    def test_returns_recent_actions_count(self, checker: PmaSafetyChecker) -> None:
        stats = checker.get_stats()
        assert "recent_actions_count" in stats
        assert "recent_actions_by_type" in stats

    def test_shows_active_circuit_breaker(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(config.circuit_breaker_threshold):
            checker.record_chat_result("agent1", "error")

        stats = checker.get_stats()
        assert stats["circuit_breaker_active"] is True
        assert stats["circuit_breaker_cooldown_remaining"] > 0


class TestRecordAction:
    def test_records_action_to_audit_log(self, checker: PmaSafetyChecker) -> None:
        entry_id = checker.record_action(
            PmaActionType.CHAT_STARTED,
            agent="agent1",
            details={"test": "value"},
        )
        assert entry_id

        recent = checker._audit_log.list_recent(limit=10)
        assert len(recent) >= 1
        assert recent[0].action_type == PmaActionType.CHAT_STARTED

    def test_records_action_with_all_fields(self, checker: PmaSafetyChecker) -> None:
        checker.record_action(
            PmaActionType.CHAT_STARTED,
            agent="agent1",
            details={"key": "value"},
            status="ok",
            error=None,
            thread_id="thread-123",
            turn_id="turn-456",
            client_turn_id="client-789",
        )

        recent = checker._audit_log.list_recent(limit=1)
        entry = recent[0]
        assert entry.agent == "agent1"
        assert entry.thread_id == "thread-123"
        assert entry.turn_id == "turn-456"
        assert entry.client_turn_id == "client-789"
        assert entry.status == "ok"


class TestIntegration:
    def test_full_flow_chat_start_success(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        checker = PmaSafetyChecker(hub_root, config=config)

        result = checker.check_chat_start("agent1", "hello")
        assert result.allowed is True

        checker.record_action(
            PmaActionType.CHAT_STARTED,
            agent="agent1",
            details={"message_truncated": "hello"[:200]},
        )
        checker.record_chat_result("agent1", "ok")

        stats = checker.get_stats()
        assert stats["circuit_breaker_active"] is False

    def test_full_flow_circuit_breaker_activation_and_recovery(
        self, hub_root: Path, config: PmaSafetyConfig
    ) -> None:
        config.circuit_breaker_cooldown_seconds = 0.1
        config.enable_dedup = False
        config.enable_rate_limit = False
        checker = PmaSafetyChecker(hub_root, config=config)

        for i in range(config.circuit_breaker_threshold):
            result = checker.check_chat_start("agent1", f"msg{i}")
            assert result.allowed is True
            checker.record_chat_result("agent1", "error")

        result = checker.check_chat_start("agent1", "blocked")
        assert result.allowed is False
        assert result.reason == "circuit_breaker_active"

        time.sleep(0.15)

        result = checker.check_chat_start("agent1", "recovered")
        assert result.allowed is True

        checker.record_chat_result("agent1", "ok")
        assert checker._failure_counts.get("chat:agent1", 0) == 0

    def test_all_layers_disabled_allows_everything(self, hub_root: Path) -> None:
        config = PmaSafetyConfig(
            enable_dedup=False,
            enable_rate_limit=False,
            enable_circuit_breaker=False,
        )
        checker = PmaSafetyChecker(hub_root, config=config)

        for _i in range(100):
            result = checker.check_chat_start("agent1", "same message")
            assert result.allowed is True
            checker.record_chat_result("agent1", "error")

        for _i in range(100):
            result = checker.check_reactive_turn()
            assert result.allowed is True
            checker.record_reactive_result(status="error")

        assert checker._is_circuit_breaker_active() is False
