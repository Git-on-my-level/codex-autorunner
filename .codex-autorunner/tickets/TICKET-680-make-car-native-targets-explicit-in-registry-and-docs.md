---
title: "Make CAR-native targets explicit in the orchestration registry and docs"
agent: "opencode"
done: true
goal: "Document and expose durable CAR-native targets such as ticket_flow and PMA as first-class orchestration-visible entries without promoting helper subsystems into fake targets."
---

## Why this ticket exists

`TICKET-180` concluded that only durable, addressable CAR-native targets should become registry-visible. This ticket makes that explicit in the orchestration catalog and the docs, while keeping dispatch interception and reactive helpers as plumbing rather than target identities.

Run order note:
- Run this ticket after `TICKET-620`, `TICKET-640`, and `TICKET-660`.

## Expected files/modules to touch

- `src/codex_autorunner/core/orchestration/catalog.py`
- `src/codex_autorunner/core/orchestration/models.py`
- `src/codex_autorunner/surfaces/web/routes/flow_routes/definitions.py`
- `src/codex_autorunner/core/self_describe/contract.py`
- `docs/adding-an-agent.md`
- `tests/core/orchestration/test_catalog.py`
- `tests/browser/test_orchestration.py`

## Tasks

- Add catalog support for explicit CAR-native target entries, including:
  - `ticket_flow` as a native flow target
  - PMA as a CAR-native orchestration-addressable agent/thread target once generalized
- Document that helper subsystems such as dispatch interception or reactive debounce logic are not standalone orchestration targets.
- Update the self-description/docs surface so orchestration-visible native targets can be described consistently.
- Keep the registry model honest about target families: thread targets versus flow targets.

## Acceptance criteria

- Durable CAR-native targets are explicitly represented in the orchestration-visible catalog and docs.
- The registry/docs distinguish native flow targets from runtime-backed thread targets.
- Helper subsystems are not misrepresented as user-addressable targets.
- The resulting description surface remains compatible with orchestration discovery by CLI/API consumers.

## Tests

- Extend `tests/core/orchestration/test_catalog.py` for native target catalog entries and target-family distinctions.
- Extend `tests/browser/test_orchestration.py` or related self-description coverage for orchestration-visible native targets.
- Run `.venv/bin/python -m pytest tests/core/orchestration/test_catalog.py tests/browser/test_orchestration.py`.
