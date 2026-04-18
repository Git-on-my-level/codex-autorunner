from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.chat_surface_lab.seeded_exploration import (
    available_seeded_perturbations,
    replay_preserved_failure_seed,
    run_seeded_exploration_campaign,
)


def test_seeded_exploration_catalog_covers_required_failure_classes() -> None:
    expected = {
        "duplicate_delivery",
        "delayed_terminal_event",
        "delete_failure",
        "retry_after_backpressure",
        "restart_window",
        "queued_submission",
    }
    assert set(available_seeded_perturbations()) == expected


@pytest.mark.anyio
async def test_failing_seed_is_preserved_and_can_be_replayed_deterministically(
    tmp_path: Path,
) -> None:
    def _deterministic_failure_validator(trial, _result):  # type: ignore[no-untyped-def]
        if trial.seed == 1701:
            raise AssertionError("forced deterministic seeded failure")

    campaign = await run_seeded_exploration_campaign(
        output_dir=tmp_path / "exploration",
        seeds=[1701],
        perturbation_ids=["duplicate_delivery"],
        result_validator=_deterministic_failure_validator,
    )

    assert campaign.passed is False
    assert campaign.failure_count == 1
    failure = campaign.failures[0]
    assert failure.seed == 1701
    assert failure.failure_path.exists()
    assert failure.scenario_path.exists()
    assert failure.run_output_dir.exists()

    failure_payload = json.loads(failure.failure_path.read_text(encoding="utf-8"))
    assert failure_payload["seed"] == 1701
    assert failure_payload["perturbation_id"] == "duplicate_delivery"
    assert "scenario" in failure_payload

    with pytest.raises(AssertionError, match="forced deterministic seeded failure"):
        await replay_preserved_failure_seed(
            failure_path=failure.failure_path,
            output_dir=tmp_path / "replay",
            result_validator=_deterministic_failure_validator,
        )
