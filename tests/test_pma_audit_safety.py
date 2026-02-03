from pathlib import Path

from codex_autorunner.core.pma_audit import PmaActionType, PmaAuditEntry, PmaAuditLog
from codex_autorunner.core.pma_safety import PmaSafetyChecker, PmaSafetyConfig


def test_pma_audit_entry_fingerprint():
    entry = PmaAuditEntry(
        action_type=PmaActionType.CHAT_STARTED,
        agent="codex",
        details={"message": "test"},
    )
    assert entry.entry_id
    assert entry.fingerprint
    assert len(entry.fingerprint) == 16


def test_pma_audit_log_append(tmp_path: Path):
    log = PmaAuditLog(tmp_path)
    entry = PmaAuditEntry(
        action_type=PmaActionType.CHAT_STARTED,
        agent="codex",
        details={"message": "test"},
    )
    entry_id = log.append(entry)
    assert entry_id == entry.entry_id
    assert log.path.exists()


def test_pma_audit_log_list_recent(tmp_path: Path):
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


def test_pma_audit_log_count_fingerprint(tmp_path: Path):
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


def test_pma_audit_log_prune(tmp_path: Path):
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


def test_pma_safety_checker_default_config():
    config = PmaSafetyConfig()
    assert config.dedup_window_seconds == 300
    assert config.max_duplicate_actions == 3
    assert config.rate_limit_window_seconds == 60
    assert config.max_actions_per_window == 20
    assert config.circuit_breaker_threshold == 5
    assert config.circuit_breaker_cooldown_seconds == 600


def test_pma_safety_checker_check_chat_start(tmp_path: Path):
    checker = PmaSafetyChecker(tmp_path)
    result = checker.check_chat_start("codex", "test message")
    assert result.allowed is True
    assert result.reason is None


def test_pma_safety_checker_dedup_blocks(tmp_path: Path):
    config = PmaSafetyConfig(
        dedup_window_seconds=300,
        max_duplicate_actions=2,
        enable_dedup=True,
    )
    checker = PmaSafetyChecker(tmp_path, config=config)
    for _ in range(2):
        checker.record_action(
            action_type=PmaActionType.CHAT_STARTED,
            agent="codex",
            details={"message_truncated": "test"},
        )
    result = checker.check_chat_start("codex", "test")
    assert result.allowed is False
    assert result.reason == "duplicate_action"


def test_pma_safety_checker_rate_limit_blocks(tmp_path: Path):
    config = PmaSafetyConfig(
        rate_limit_window_seconds=60,
        max_actions_per_window=2,
        enable_rate_limit=True,
    )
    checker = PmaSafetyChecker(tmp_path, config=config)
    for i in range(2):
        checker.check_chat_start("codex", f"message-{i}")
    result = checker.check_chat_start("codex", "message-3")
    assert result.allowed is False
    assert result.reason == "rate_limit_exceeded"


def test_pma_safety_checker_circuit_breaker(tmp_path: Path):
    config = PmaSafetyConfig(
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown_seconds=600,
        enable_circuit_breaker=True,
    )
    checker = PmaSafetyChecker(tmp_path, config=config)
    for _ in range(2):
        checker.record_chat_result("codex", "error", error="test error")
    result = checker.check_chat_start("codex", "test message")
    assert result.allowed is False
    assert result.reason == "circuit_breaker_active"


def test_pma_safety_checker_reset_circuit_breaker(tmp_path: Path):
    config = PmaSafetyConfig(
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown_seconds=1,
        enable_circuit_breaker=True,
    )
    checker = PmaSafetyChecker(tmp_path, config=config)
    for _ in range(2):
        checker.record_chat_result("codex", "error", error="test error")
    assert not checker.check_chat_start("codex", "test").allowed
    import time

    time.sleep(1.1)
    assert checker.check_chat_start("codex", "test").allowed
    assert checker._failure_counts == {}
    checker.record_chat_result("codex", "error", error="test error")
    assert checker.check_chat_start("codex", "test").allowed


def test_pma_safety_checker_stats(tmp_path: Path):
    checker = PmaSafetyChecker(tmp_path)
    checker.record_action(action_type=PmaActionType.CHAT_STARTED, agent="codex")
    checker.record_action(action_type=PmaActionType.CHAT_COMPLETED, agent="codex")
    stats = checker.get_stats()
    assert stats["circuit_breaker_active"] is False
    assert stats["recent_actions_count"] == 2
    assert "chat_started" in stats["recent_actions_by_type"]
    assert "chat_completed" in stats["recent_actions_by_type"]
