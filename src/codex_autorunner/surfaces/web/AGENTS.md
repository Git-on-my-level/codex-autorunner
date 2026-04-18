# Web Surface - Agent Guide

This surface owns FastAPI routes, web-specific services, and static asset serving for the UI.

## Route Here When

- The task changes HTTP routes, SSE/websocket behavior, app wiring, or static asset refresh/cache behavior.
- A frontend change also needs server-side payload or route updates.

## Keep Straight

- Route handlers live under `routes/`; prefer extending the closest route package before growing `app.py`.
- Shared web-specific logic belongs in `services/`, `static_assets.py`, `static_refresh.py`, or nearby helpers.
- UI source lives in `../../static_src/`; compiled assets live in `../../static/`.
- Broader surface overview: `README.md`.

## Tests

- Primary web surface tests: `tests/surfaces/web/`
- Web-ui lane tests also include root files such as `tests/test_static_asset_cache.py`, `tests/test_auth_middleware.py`, `tests/test_hub_ui_escape.py`, and `tests/test_voice_ui.py`.
- For broader test routing, read `tests/AGENTS.md`.
