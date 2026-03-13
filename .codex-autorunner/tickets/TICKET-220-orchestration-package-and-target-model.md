---
title: "Create the orchestration package, target model, and shared naming layer"
agent: "codex"
done: true
goal: "Add the shared orchestration package and codify the target model so CAR stops treating PMA managed-thread terms as the architectural center."
---

## Why this ticket exists

`TICKET-100` locked the orchestration vocabulary in `.codex-autorunner/contextspace/orchestration-contract-target-model.md`. This ticket creates the package and type layer that future services, migrations, and surfaces can depend on.

Run order note:
- Run this ticket first in the downstream implementation set.

## Expected files/modules to touch

- `src/codex_autorunner/core/orchestration/__init__.py`
- `src/codex_autorunner/core/orchestration/models.py`
- `src/codex_autorunner/core/orchestration/catalog.py`
- `src/codex_autorunner/agents/types.py`
- `tests/core/orchestration/test_orchestration_models.py`
- `tests/core/orchestration/test_catalog.py`

## Tasks

- Create `src/codex_autorunner/core/orchestration/` as the shared package for orchestration-core code.
- Define the orchestration-visible types from the TICKET-100 memo:
  - `AgentDefinition`
  - `ThreadTarget`
  - `MessageRequest`
  - `ExecutionRecord`
  - `FlowTarget`
  - `Binding`
  - `TargetCapability`
- Keep `ConversationRef` and `TurnRef` as runtime-adapter handles in `src/codex_autorunner/agents/types.py`; do not rename them into orchestration nouns.
- Add a small catalog/helper layer that can normalize registry-backed agent definitions without introducing PMA-specific types.
- Export the new types cleanly from the package so later tickets can import them from one stable module path.

## Acceptance criteria

- `src/codex_autorunner/core/orchestration/` exists and provides the target-model types defined by the TICKET-100 memo.
- The code clearly distinguishes orchestration nouns from runtime-adapter nouns.
- No new type makes PMA managed-thread terminology the default orchestration vocabulary.
- The package is minimal and dependency-safe for later tickets; it does not pull in web or CLI modules.

## Tests

- Add unit coverage in `tests/core/orchestration/test_orchestration_models.py` for normalization, defaults, and any serialization helpers.
- Add unit coverage in `tests/core/orchestration/test_catalog.py` showing registry descriptors can be mapped to orchestration-visible definitions without PMA special cases.
- Run `.venv/bin/python -m pytest tests/core/orchestration/test_orchestration_models.py tests/core/orchestration/test_catalog.py`.

## Progress

- Added the shared orchestration package under `src/codex_autorunner/core/orchestration/` with stable exports from `__init__.py`.
- Implemented the orchestration target-model dataclasses in `models.py` and kept `ConversationRef` / `TurnRef` in `src/codex_autorunner/agents/types.py` as runtime-native handles.
- Added `catalog.py` helpers that normalize `AgentDescriptor` records into orchestration-facing `AgentDefinition` values without PMA-specific types.
- Added focused unit coverage in `tests/core/orchestration/test_orchestration_models.py` and `tests/core/orchestration/test_catalog.py`.

## Outcome

- The orchestration naming layer now exists as a dependency-safe core package for later service, migration, and surface tickets.
- The code distinguishes orchestration nouns (`ThreadTarget`, `ExecutionRecord`, `Binding`, `FlowTarget`) from runtime adapter nouns (`ConversationRef`, `TurnRef`).
- Validation passed with `.venv/bin/python -m pytest tests/core/orchestration/test_orchestration_models.py tests/core/orchestration/test_catalog.py`.
