from __future__ import annotations

from codex_autorunner.core.retry import _compute_exponential_retry_delay


def test_compute_exponential_retry_delay_applies_jitter_within_cap(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.core.retry.random.uniform",
        lambda start, end: end,
    )

    delay = _compute_exponential_retry_delay(
        attempt_number=2,
        base_wait=1.0,
        max_wait=10.0,
        jitter=0.25,
    )

    assert delay == 2.5


def test_compute_exponential_retry_delay_respects_max_wait_cap(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "codex_autorunner.core.retry.random.uniform",
        lambda start, end: end,
    )

    delay = _compute_exponential_retry_delay(
        attempt_number=10,
        base_wait=1.0,
        max_wait=10.0,
        jitter=0.5,
    )

    assert delay == 10.0
