---
title: "Add authoritative binding storage and cross-surface query support"
agent: "codex"
done: true
goal: "Move thread-binding authority into orchestration-owned storage and add shared queries for bindings and active work across PMA, Discord, Telegram, and web."
---

## Why this ticket exists

`TICKET-160` identified `src/codex_autorunner/core/chat_bindings.py` as a derived read layer over PMA and transport stores rather than the long-term authority. This ticket establishes orchestration-owned binding storage and the shared query layer that later CLI/web/transport surfaces can consume.

Run order note:
- Run this ticket after `TICKET-320` and `TICKET-520`.
- Land this binding authority work before the later PMA and query/UX follow-through.

## Expected files/modules to touch

- `src/codex_autorunner/core/orchestration/bindings.py`
- `src/codex_autorunner/core/orchestration/sqlite.py`
- `src/codex_autorunner/core/orchestration/service.py`
- `src/codex_autorunner/core/chat_bindings.py`
- `src/codex_autorunner/integrations/telegram/state.py`
- `src/codex_autorunner/integrations/discord/chat_state_store.py`
- `tests/core/orchestration/test_bindings.py`
- `tests/core/test_chat_bindings.py`
- `tests/test_telegram_state.py`
- `tests/integrations/discord/test_state_store.py`

## Tasks

- Add authoritative binding CRUD and lookup support in the orchestration package backed by hub SQLite.
- Enforce one active thread binding per surface context while preserving transport-owned delivery state in Discord and Telegram databases.
- Add shared queries for:
  - binding by surface key
  - bindings by repo
  - bindings by agent
  - active thread for a binding
  - active work summaries by agent and repo
- Refactor `src/codex_autorunner/core/chat_bindings.py` into a compatibility/query layer over orchestration-owned bindings instead of a multi-store authority.
- Keep compatibility adapters where needed during migration, but do not leave Discord/Telegram topic records or PMA thread rows as the authoritative source of bound thread identity.

## Acceptance criteria

- Hub SQLite owns authoritative transport-agnostic binding rows.
- CAR can answer active-binding and active-work queries without relying on PMA-only or transport-specific authority.
- One-thread-per-binding semantics are explicit and enforced at the orchestration binding layer.
- Discord and Telegram state stores remain authoritative only for transport-local delivery and metadata concerns.

## Tests

- Add coverage in `tests/core/orchestration/test_bindings.py` for binding creation, replacement, disablement, and one-thread-per-surface enforcement.
- Extend `tests/core/test_chat_bindings.py` to show compatibility queries read orchestration-owned bindings correctly.
- Extend `tests/test_telegram_state.py` and `tests/integrations/discord/test_state_store.py` to verify transport stores no longer need to be the authority for bound thread identity.
- Run `.venv/bin/python -m pytest tests/core/orchestration/test_bindings.py tests/core/test_chat_bindings.py tests/test_telegram_state.py tests/integrations/discord/test_state_store.py`.

## Progress

- Added `src/codex_autorunner/core/orchestration/bindings.py` with authoritative hub-SQLite binding CRUD, one-active-binding-per-surface replacement behavior, active-thread lookup, and active-work summary queries.
- Updated orchestration service/builders to expose the new binding query seam without changing the runtime-thread harness contract.
- Refactored `src/codex_autorunner/core/chat_bindings.py` into an orchestration-first compatibility layer: PMA counts still come from orchestration thread metadata, orchestration-owned `orch_bindings` rows now drive Discord/Telegram binding counts, and legacy transport timestamps remain as migration-era freshness metadata.
- Clarified the authority boundary in the Discord and Telegram state adapters so they remain transport-local metadata/delivery stores rather than the long-term home for bound thread identity.
- Added binding/query regression coverage in `tests/core/orchestration/test_bindings.py`, extended compatibility coverage in `tests/core/test_chat_bindings.py`, and added transport-side regression checks in `tests/test_telegram_state.py` and `tests/integrations/discord/test_state_store.py`.

## Outcome

- Hub SQLite now owns authoritative transport-agnostic binding rows through `orch_bindings`, and one-thread-per-surface semantics are enforced at the orchestration binding layer.
- CAR can answer binding-by-surface, bindings-by-agent/repo, active-thread-for-binding, and active-work-by-agent/repo queries from orchestration-owned metadata.
- `src/codex_autorunner/core/chat_bindings.py` remains available as a compatibility/query layer, but it now prefers orchestration-owned bindings over transport-state aggregation and only falls back to legacy transport stores where needed during migration.
- Validation passed with:
  - `.venv/bin/python -m pytest tests/core/orchestration/test_bindings.py tests/core/test_chat_bindings.py tests/test_telegram_state.py tests/integrations/discord/test_state_store.py`
  - `.venv/bin/python -m pytest tests/core/orchestration/test_service.py tests/core/orchestration/test_sqlite_migrations.py tests/core/orchestration/test_orchestration_models.py`
  - `.venv/bin/python -m ruff check src/codex_autorunner/core/orchestration/bindings.py src/codex_autorunner/core/orchestration/service.py src/codex_autorunner/core/orchestration/migrations.py src/codex_autorunner/core/chat_bindings.py src/codex_autorunner/integrations/telegram/state.py src/codex_autorunner/integrations/discord/chat_state_store.py tests/core/orchestration/test_bindings.py tests/core/orchestration/test_service.py tests/core/test_chat_bindings.py tests/test_telegram_state.py tests/integrations/discord/test_state_store.py`
