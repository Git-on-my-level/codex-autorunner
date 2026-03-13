---
title: "Expose orchestration capability discovery and capability-aware CLI filtering"
agent: "opencode"
done: true
goal: "Publish capability metadata through orchestration-friendly APIs and make CLI/web surfaces consume that metadata instead of hard-coded PMA assumptions."
---

## Why this ticket exists

CAR already has backend capability metadata in `src/codex_autorunner/agents/registry.py`, but the metadata is not yet exposed as a shared orchestration-facing contract. This ticket does the straightforward surfacing and filtering work once the orchestration package exists.

Run order note:
- Run this ticket after `TICKET-220` and `TICKET-240`.
- Land this capability surface before the later runtime-thread integration work.

## Expected files/modules to touch

- `src/codex_autorunner/agents/registry.py`
- `src/codex_autorunner/core/orchestration/catalog.py`
- `src/codex_autorunner/surfaces/web/routes/agents.py`
- `src/codex_autorunner/surfaces/web/routes/pma_routes/meta.py`
- `src/codex_autorunner/surfaces/cli/pma_cli.py`
- `tests/routes/test_agents_routes.py`
- `tests/test_pma_routes.py`
- `tests/test_pma_cli.py`

## Tasks

- Extend the orchestration catalog layer so surfaces can read normalized capability metadata from one place.
- Surface capability metadata through `/api/agents` and `/hub/pma/agents` instead of only returning ad hoc availability/name fields.
- Update `car pma agents` and related PMA CLI output to display capability-aware metadata.
- Filter or annotate CLI/web affordances based on capability flags such as review support, model listing, interrupt support, approvals, and event streaming.
- Preserve the richer adapter capability surface; do not reduce everything to a binary “available/unavailable” check.

## Acceptance criteria

- Capability metadata is exposed through a shared orchestration-facing path rather than duplicated PMA-only logic.
- CLI and web agent listings reflect capability differences between adapters.
- The work remains capability-driven and does not hard-code Codex/OpenCode behavior into surface code.
- No part of this ticket describes `ticket_flow` as a conversational thread target.

## Tests

- Add route coverage in `tests/routes/test_agents_routes.py` and `tests/test_pma_routes.py` for capability fields and filtering behavior.
- Add CLI coverage in `tests/test_pma_cli.py` for capability-aware output.
- Run `.venv/bin/python -m pytest tests/routes/test_agents_routes.py tests/test_pma_routes.py tests/test_pma_cli.py`.
