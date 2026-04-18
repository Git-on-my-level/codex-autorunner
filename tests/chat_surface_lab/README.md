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
  `interrupt_active_turn`, duplicate-delivery actions, etc.)
- `faults`: deterministic perturbations (for example short timeout overrides)
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
```

## Adding a scenario

1. Add a new JSON file under `tests/chat_surface_lab/scenarios/`.
2. Reuse existing action/fault/invariant shapes from nearby scenarios.
3. Link relevant IDs in `contract_links.regression_ids` and
   `contract_links.latency_budget_ids` when the scenario maps to the shared UX
   regression contract.
4. Extend runner support if a new action kind is required.
5. Keep assertions at transcript/log level so scenarios stay reusable across
   surface-specific test files.
