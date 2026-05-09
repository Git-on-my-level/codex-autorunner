# Web Surface - Agent Guide

This surface owns FastAPI routes, web-specific services, and static asset serving for the UI.

## Route Here When

- The task changes HTTP routes, SSE/websocket behavior, app wiring, or static asset refresh/cache behavior.
- A frontend change also needs server-side payload or route updates.

## Keep Straight

- Route handlers live under `routes/`; prefer extending the closest route package before growing `app.py`.
- Shared web-specific logic belongs in `services/`, `static_assets.py`, `static_refresh.py`, or nearby helpers.
- PMA Hub UI: `../../pma_frontend/` → `../../pma_static/`.
- PMA managed-thread chat routes expose backend-owned timeline, progress, queue, and delivery state from `adapters/chat/` and `core/orchestration/`. Do not add web-local transcript composition or delivery state machines.
- Broader surface overview: `README.md`.

### PMA Hub SPA shell (deep links)

The PMA UI is a static SvelteKit bundle: every **document** request to a URL the
client router can show must return `pma_static/index.html`. In-app navigation
only updates history; a **refresh** or **open in new tab** requests that path from
the hub. With no matching `GET`, FastAPI returns JSON (for example
`{"detail":"Not Found"}`) and the tab can look blank or show the browser JSON
viewer.

- When you add a **new top-level path** under `pma_frontend/src/routes/`, add
  hub `GET` routes in `app.py` that return `_pma_index_response()` (search for
  `PMA Hub SPA shell`).
- For segments that **can nest** (for example `/chats/...`), use
  `@app.get("/segment/{rest:path}")` alongside `@app.get("/segment")` so nested
  paths stay covered without listing each shape.
- Add at least one deep sample path to `tests/surfaces/web/test_pma_static_routes.py`
  when you introduce a new subtree so CI enforces the contract.
- Run `python scripts/check_pma_hub_spa_shell.py` after adding or renaming
  `pma_frontend/src/routes/**/+page.svelte` trees (also runs in `scripts/check.sh`
  web-ui lane after `pnpm pma:test`).

## Tests

- Primary web surface tests: `tests/surfaces/web/`
- Web-ui lane tests also include root files such as `tests/test_static_asset_cache.py` and `tests/test_auth_middleware.py`.
- For broader test routing, read `tests/AGENTS.md`.
