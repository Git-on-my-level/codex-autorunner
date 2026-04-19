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
3. Runs the deterministic latency-budget suite and writes diagnostics.

The suite uses local harness fixtures only; it does not require live Telegram
or Discord credentials.

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
