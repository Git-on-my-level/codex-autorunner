# RFC: Move surface/adapter flows out of core (issue #407)

## Summary
Surface workflows (doc chat/review/snapshot) and backend glue live under `src/codex_autorunner/core/`, blurring the Architecture Map layers. This RFC records the desired boundaries and migration steps to relocate surface/adapter logic.

## Problem statement
- Architecture Map: Surfaces → Adapters → Control Plane → Engine (one-way).
- `core/doc_chat.py`, `core/review.py`, `core/snapshot.py`, and `core/app_server_events.py` import OpenCode runtime and app-server supervisors/clients.
- Core therefore mixes engine/control-plane code with surface/backend orchestration.

## Goals
- Keep `core/` limited to engine + control-plane primitives (artifact/state, scheduling).
- Place backend orchestration in `integrations/<backend>/`.
- Place UX workflows in `surfaces/<surface>/` or CLI layers.
- Prevent reverse dependencies back into `core/`.

## Proposed steps
1) Carve out packages: `core/engine`, `core/control_plane`, `integrations/<backend>/`, `surfaces/web|telegram|cli`.
2) Move doc chat/review/snapshot orchestration into surface/adapter packages; keep only persistence/indexing helpers in core.
3) Extract app-server streaming glue (`app_server_events`) into the app-server adapter.
4) Add lightweight import guard to ensure `core/*` does not import `integrations/*` or surface modules.
5) Add README per package summarizing responsibilities and allowed dependencies.

## Acceptance criteria
- `core/` contains only engine/control-plane modules.
- Doc chat/review/snapshot/app-server glue reside in surface/adapter packages.
- Imports respect one-way dependency rule.

## Tracking
Fixes #407.
