# Chat Surface Lab

Shared contracts for the chat-surface lab initiative.

This package is the discoverable home for the test-only models that later
tickets will use to build a deterministic Telegram and Discord surface lab. The
goal is to keep future work anchored to one obvious package instead of
recreating scenario, transcript, and artifact contracts inside individual test
files.

## Current scope

TICKET-100 adds the package skeleton only:

- `scenario_models.py` defines declarative scenario metadata
- `transcript_models.py` defines normalized transcript and timeline records
- `artifact_manifests.py` defines artifact bundle metadata
- `backend_runtime.py` defines the backend-neutral fixture runtime contract used
  by lab and surface tests to drive Codex app-server, Hermes/ACP, and OpenCode
  fixtures through one normalized control seam

These modules are intentionally lightweight. They provide importable contracts
for later tickets without changing existing integration behavior.

TICKET-130 adds semantic surface simulators:

- `telegram_simulator.py` for deterministic Telegram behavior + transcript
  normalization
- `discord_simulator.py` for deterministic Discord interaction/message behavior
  + transcript normalization

TICKET-140 adds a declarative scenario corpus and runner:

- `scenarios/*.json` stores reusable regression scenarios
- `scenario_runner.py` loads/validates scenarios, executes them against shared
  surface harnesses, asserts transcript invariants + latency budgets, and emits
  artifact bundles
- `test_scenario_corpus.py` validates corpus completeness and contract linkage
- `test_scenario_runner.py` validates real scenario execution and artifact
  outputs

TICKET-150 adds evidence artifact rendering and indexing:

- `transcript_renderer.py` renders deterministic `transcript.html` pages from
  normalized transcript events
- `evidence_artifacts.py` writes stable bundle outputs
  (`transcript.json`, `timeline.json`, `logs.json`, `timing_report.json`,
  `transcript.html`, `manifest.json`) and captures browser evidence artifacts
  (`screenshot.png`, `a11y_snapshot.json`) through
  `src/codex_autorunner/browser/runtime.py`
- `test_transcript_renderer.py` validates deterministic HTML + event payload
  rendering
- `test_artifact_manifest.py` validates required manifest entries, stable
  filenames, and failed-run artifact preservation

TICKET-160 adds a deterministic latency budget suite runner:

- `latency_budget_runner.py` executes named lab scenarios and enforces the
  shared UX latency budgets from
  `src/codex_autorunner/integrations/chat/ux_regression_contract.py`
- the runner writes machine-readable artifacts under
  `.codex-autorunner/diagnostics/chat-latency-budgets/` with
  `latest.json`, `history/*`, and per-run `runs/<run_id>/suite_report.json`
- `scripts/chat_surface_latency_budgets.py` provides an operator-friendly
  entrypoint (also exposed via `make perf-chat-latency-budgets`)
- `test_latency_budgets.py` validates required budget coverage and
  failure-triage payloads

TICKET-170 adds seeded exploration and incident replay tooling:

- `seeded_exploration.py` executes deterministic seed+perturbation campaigns
  across the scenario runner and preserves failing seeds with replay bundles
- `incident_replay.py` converts incident traces into sanitized replay scenario
  fixtures that load directly through the corpus parser
- `scenarios/restart_window_duplicate_delivery.json` adds executable
  restart-window duplicate-delivery coverage in the corpus
- `scripts/chat_surface_seeded_exploration.py` runs the seeded exploration
  campaign from CLI (also exposed via `make perf-chat-seeded-exploration`)
- `scripts/chat_surface_incident_replay.py` converts an incident JSON payload
  into a sanitized scenario fixture
- `test_seeded_exploration.py` validates failure-seed preservation and
  deterministic replay
- `test_incident_replay.py` validates trace-to-scenario conversion and
  sanitization

## Relationship to nearby packages

- `tests.chat_surface_lab`
  Owns shared lab contracts and package-level documentation.
- `tests.chat_surface_harness`
  Owns reusable Telegram, Discord, and Hermes test harness helpers.
- `tests.chat_surface_integration`
  Owns today’s high-signal end-to-end chat regression suites.

The intended layering is:

1. `chat_surface_lab` declares shared contracts.
2. `chat_surface_harness` provides service-facing fixture helpers.
3. `chat_surface_integration` and future lab runners consume both.

## Design constraints

- Use real surface entrypoints rather than a parallel fake business stack.
- Keep backend runtime modeling transport-neutral so Hermes, ACP, app-server,
  and later OpenCode fixtures can share the same scenario contract.
- Prefer deterministic, diffable artifact shapes over ad hoc assertions.

## Scenario DSL quick reference

Scenarios are JSON files under `tests/chat_surface_lab/scenarios/` with these
high-signal fields:

- `scenario_id`: explicit stable ID used by corpus and tests
- `surfaces`: one or more of `discord` / `telegram`
- `runtime_fixture`: backend fixture kind + fixture scenario
- `actions`: declarative inbound flow (`send_message`, `start_message`,
  `wait_for_running_execution`, `submit_active_message`,
  `interrupt_active_turn`, duplicate-delivery actions, restart-window actions
  such as `restart_surface_harness`, and status replay actions such as
  `run_status_interaction` / `run_status_update`)
- `faults`: deterministic perturbations (for example short timeout overrides)
  including `retry_after` schedules and delivery cleanup failures
- `transcript_invariants`: transcript and log assertions
- `latency_budgets`: budget assertions bound to concrete timing log fields
- `contract_links`: optional mapping to
  `src/codex_autorunner/integrations/chat/ux_regression_contract.py`
- `execution_mode`: `surface_harness` (default) or `reference_only`

## Running the corpus

Run focused DSL checks:

```bash
.venv/bin/python -m pytest tests/chat_surface_lab/test_scenario_runner.py -q
.venv/bin/python -m pytest tests/chat_surface_lab/test_scenario_corpus.py -q
.venv/bin/python -m pytest tests/chat_surface_lab/test_transcript_renderer.py -q
.venv/bin/python -m pytest tests/chat_surface_lab/test_artifact_manifest.py -q
.venv/bin/python -m pytest tests/chat_surface_lab/test_latency_budgets.py -q
.venv/bin/python -m pytest tests/chat_surface_lab/test_seeded_exploration.py -q
.venv/bin/python -m pytest tests/chat_surface_lab/test_incident_replay.py -q

# Operator run (outside raw pytest)
make perf-chat-latency-budgets
make perf-chat-seeded-exploration
```

Incident replay conversion:

```bash
.venv/bin/python scripts/chat_surface_incident_replay.py \
  --incident /path/to/incident.json \
  --output tests/chat_surface_lab/scenarios/incident_replay_example.json \
  --scenario-id incident_replay_example
```

## Evidence artifact schema

Each executable scenario surface run writes an `artifacts/manifest.json` index
with deterministic paths relative to that `artifacts/` directory.

Stable artifact filenames:

- `transcript.json`: normalized transcript schema (`schema_version=1`)
- `timeline.json`: raw surface operation timeline (`schema_version=1`)
- `transcript.html`: deterministic transcript renderer output for visual diffs
- `logs.json`: structured log records captured during scenario execution
- `timing_report.json`: derived timing metrics from `chat_ux_delta_*` fields
- `manifest.json`: artifact index + browser capture status
- `screenshot.png`: browser screenshot of `transcript.html` (when capture
  succeeds)
- `a11y_snapshot.json`: browser accessibility snapshot of `transcript.html`
  (when capture succeeds)

## Adding a scenario

1. Add a new JSON file under `tests/chat_surface_lab/scenarios/`.
2. Reuse existing action/fault/invariant shapes from nearby scenarios.
3. Link relevant IDs in `contract_links.regression_ids` and
   `contract_links.latency_budget_ids` when the scenario maps to the shared UX
   regression contract.
4. Extend runner support if a new action kind is required.
5. Keep assertions at transcript/log level so scenarios stay reusable across
   surface-specific test files.
