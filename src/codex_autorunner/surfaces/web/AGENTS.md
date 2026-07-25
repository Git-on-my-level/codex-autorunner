# Web Surface - Agent Guide

This surface owns FastAPI routes, web-specific services, and static asset serving for the UI.

## Route Here When

- The task changes HTTP routes, SSE/websocket behavior, app wiring, or static asset refresh/cache behavior.
- A frontend change also needs server-side payload or route updates.

## Keep Straight

- Route handlers live under `routes/`; prefer extending the closest route package before growing `app.py`.
- Shared web-specific logic belongs in `services/`, `static_assets.py`, `static_refresh.py`, or nearby helpers.
- Web Hub UI: `../../web_frontend/` → ignored package artifact
  `../../web_static/`. Do not require generated static assets in normal PRs;
  release/package builds produce and verify them.
- PMA managed-thread chat routes expose the backend-owned transcript projection plus progress, queue, and delivery state from `adapters/chat/` and `core/orchestration/`. The Web Chat primary rendering contract is `/hub/pma/threads/{id}/transcript` and `/transcript/events`; `/tail` and `/timeline` are diagnostics/projection inputs. Do not add web-local transcript composition or delivery state machines.
- Web UI screen routes should expose screen-shaped read models with cursors and
  repair policies. Keep contracts in `read_model_contracts.py`, route assembly
  under `routes/`, and projection/rebuild logic in `core/` near the canonical
  data source.
- Do not add new broad list endpoints or normal polling endpoints as primary UI
  data sources when a scoped read model would fit. Existing broad endpoints must
  remain diagnostics/tests-only or have a documented migration exception.
- Every stream event must be typed, ordered within its source, idempotent, and
  repairable from a snapshot route. Cursor gaps must be explicit.
- For a new high-cardinality read model, add route coverage for bounded windows
  and extend the web responsiveness budget smoke where appropriate.
- Broader surface overview: `README.md`.

### Web Hub SPA shell (deep links)

The Web UI is a static SvelteKit bundle: every **document** request to a URL the
client router can show must return `web_static/index.html`. In-app navigation
only updates history; a **refresh** or **open in new tab** requests that path from
the hub. With no matching route, FastAPI returns JSON (for example
`{"detail":"Not Found"}`) and the tab can look blank or show the browser JSON
viewer.

**The SvelteKit filesystem is the single source of truth for this**, not a
hand-maintained list in `app.py`:

- `web_frontend/scripts/generate-spa-routes.mjs` runs as part of
  `pnpm --filter @codex-autorunner/web-hub build` (i.e. `pnpm web:build`). It
  walks `web_frontend/src/routes/**/+page.svelte` and writes
  `web_static/spa_routes.json` — a manifest of Starlette path templates
  (`[foo]` → `{foo}`, `[[foo]]` → both with and without the segment,
  `[...foo]` → `{foo:path}`). `web_static/` ships in the Python package (see
  `pyproject.toml` `[tool.setuptools.package-data]`); `web_frontend/src/routes/`
  does not, which is why the manifest — not the source tree — is what `app.py`
  reads at runtime.
- `app.py`'s `_register_spa_shell_routes` reads that manifest at app
  construction and registers a shell-serving route per template. **Adding,
  removing, or renaming a `+page.svelte` route requires no `app.py` change** —
  just rebuild.
- Two things stay intentionally hand-maintained in `app.py` because they are
  not derivable from the filesystem:
  - `_NEST_TOLERANT_SPA_SEGMENTS` (`chats`, `services`, `automations`,
    `settings`): top-level sections that tolerate arbitrary nested paths
    beyond any single `+page.svelte` (a product decision, not a routing fact).
  - Any manifest path containing a literal `worktrees` segment: these need
    parent-scope validation or a legacy redirect, not a blanket shell
    response, so they keep bespoke handlers below the manifest registration.
- If the manifest is missing or unreadable (partial/old build), app
  construction does not crash — it logs a warning and falls back to broad
  shell coverage for `repos/`, `tickets/`, `contextspace/` so a stale build
  degrades to "too broad" rather than "blank tab."
- `scripts/check_web_hub_spa_shell.py` recomputes the expected route set
  directly from `web_frontend/src/routes/` and diffs it against the shipped
  manifest — it only catches "forgot to `pnpm web:build` after changing
  routes," it does not boot the hub app. Runtime correctness (every
  filesystem route actually resolves to a 200 shell or the legacy redirect)
  is proven by `tests/surfaces/web/test_web_static_routes.py::test_every_filesystem_spa_route_serves_shell_or_legacy_redirect`,
  which walks a live `TestClient`.
- Run `pnpm web:build` (regenerates the manifest) then
  `python scripts/check_web_hub_spa_shell.py` after adding or renaming
  `web_frontend/src/routes/**/+page.svelte` trees (the checker also runs in
  `scripts/check.sh` web-ui lane after `pnpm run build`).

## Tests

- Primary web surface tests: `tests/surfaces/web/`
- Web-ui lane tests also include root files such as `tests/test_static_asset_cache.py` and `tests/test_auth_middleware.py`.
- For broader test routing, read `tests/AGENTS.md`.
