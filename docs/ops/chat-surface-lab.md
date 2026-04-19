# Chat Surface Lab Runbook

This runbook is the operator and agent entrypoint for deterministic chat-surface
lab validation.

## Single Entry Command

Run the repo-level lab suite:

```bash
make test-chat-surface-lab
```

What this command does:

1. Validates the committed scenario corpus contract.
2. Validates stable artifact manifest expectations.
3. Runs the latency-budget suite tests.
4. Runs seeded exploration and incident replay tests.
5. Writes latency-budget diagnostics under
   `.codex-autorunner/diagnostics/chat-latency-budgets/`.

The suite uses local harness fixtures only; it does not require live Telegram
or Discord credentials.

## Campaign Gate Usage

Campaign tickets use the chat-surface lab as their acceptance boundary. To
determine whether the campaign north star is green or red:

1. Run `make test-chat-surface-lab`.
2. Check the `campaign_north_star` section in the suite report
   (`latest.json`). If `green` is `true`, all required latency budgets pass
   and all required scenario IDs are covered.
3. Alternatively, import and call
   `campaign_north_star_status()` from
   `src/codex_autorunner/integrations/chat/ux_regression_contract.py`
   with observed budgets and scenario IDs.

### Campaign north star thresholds

The campaign considers these latency thresholds hard gates:

| Budget ID | Threshold |
|---|---|
| `first_visible_feedback` | <= 1500 ms |
| `queue_visible` | <= 1500 ms |
| `first_semantic_progress` | <= 5000 ms |
| `interrupt_visible` | <= 1500 ms |

### Campaign-critical scenario matrix

| Scenario ID | Primary Budget | Gating |
|---|---|---|
| `first_visible_feedback` | `first_visible_feedback`, `first_semantic_progress` | gating |
| `queued_visibility` | `queue_visible` | gating |
| `progress_anchor_reuse` | (none) | non-gating |
| `interrupt_optimistic_acceptance` | `interrupt_visible` | gating |
| `interrupt_confirmation` | (none) | gating |
| `restart_recovery` | (none) | non-gating |
| `duplicate_delivery` | (none) | gating |

`fast_ack` is a required regression scenario covered by
`interrupt_optimistic_acceptance` via contract link aliasing. When
`interrupt_optimistic_acceptance` is observed in the suite, `fast_ack` is
considered covered for the campaign north star.

Non-gating scenarios contribute to corpus coverage but do not have latency
budget assertions that block the campaign north star. If a non-gating scenario
later acquires a product-critical latency budget, promote it to gating in
`CAMPAIGN_CRITICAL_SCENARIO_MATRIX`.

### How to use this as a ticket acceptance boundary

1. Before closing a campaign implementation ticket, run
   `make test-chat-surface-lab` and verify the campaign north star is green.
2. If the north star is red, the suite report names the failing budget IDs and
   scenario IDs. Fix the regression before closing the ticket.
3. If a scenario or budget cannot be hard-gated yet, document why in the
   ticket and leave a clear non-gating reporting path in
   `CAMPAIGN_CRITICAL_SCENARIO_MATRIX`.

## Artifact Locations

`make test-chat-surface-lab` writes latency-budget diagnostics under:

```bash
.codex-autorunner/diagnostics/chat-latency-budgets/
```

Important paths:

- `latest.json`: most recent suite report.
- `history/*.json`: timestamped run history snapshots.
- `runs/<run_id>/suite_report.json`: run-specific report.
- `runs/<run_id>/<scenario_id>/<surface>/artifacts/`: per-surface evidence
  (`transcript.json`, `timeline.json`, `logs.json`, `timing_report.json`,
  `transcript.html`, `manifest.json`, plus browser captures when available).

## Add a New Scenario

1. Create a new JSON file in `tests/chat_surface_lab/scenarios/`.
2. Set a stable `scenario_id` and explicit `surfaces`.
3. Declare deterministic `actions`, optional `faults`, and transcript
   invariants.
4. Add `latency_budgets` entries when the scenario asserts UX timing behavior.
5. Link contract IDs in `contract_links.regression_ids` and
   `contract_links.latency_budget_ids` when applicable.
6. Run `make test-chat-surface-lab` and confirm artifacts are emitted.

If you introduce a new action or fault kind, extend
`tests/chat_surface_lab/scenario_runner.py` and add focused tests near existing
runner/corpus tests.

## Convert an Incident to a Replay Scenario

Use the incident replay converter:

```bash
.venv/bin/python scripts/chat_surface_incident_replay.py \
  --incident /path/to/incident.json \
  --output tests/chat_surface_lab/scenarios/incident_replay_example.json \
  --scenario-id incident_replay_example
```

Workflow:

1. Capture the real incident payload.
2. Convert it with `chat_surface_incident_replay.py` (sanitizes and normalizes).
3. Review/edit generated scenario details and contract links.
4. Run `make test-chat-surface-lab`.
5. Keep the scenario in the corpus to make the incident replayable in CI and
   future doctor checks.

## CI and Doctor Integration

- `./scripts/check.sh --lane chat-apps` runs `make test-chat-surface-lab`.
- CI `chat-apps` and `aggregate` jobs run the same lane checks.
- `car doctor --dev` includes chat-surface-lab contract diagnostics:
  scenario corpus health, contract-link coverage, and artifact contract checks.

## Campaign Scorecard API

Python callers can import the campaign scorecard directly:

```python
from codex_autorunner.integrations.chat.ux_regression_contract import (
    campaign_north_star_status,
    format_campaign_scorecard,
)

status = campaign_north_star_status(
    observed_budgets=[...],  # list of {"budget_id": ..., "observed_ms": ...}
    observed_scenario_ids=[...],
)

print(format_campaign_scorecard(status))
# => GREEN if all budgets pass and all required scenarios are covered
```

The scorecard output is designed to be citable from ticket evidence.
