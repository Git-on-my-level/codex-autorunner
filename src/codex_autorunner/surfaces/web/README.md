# Web Surface

FastAPI web UI, API routes, and web-specific workflows.

## Responsibilities

- Render web UI and provide REST API
- Handle HTTP/websocket connections
- Stream real-time events via SSE
- Provide web-based ergonomics (logs, dashboards)

## Allowed Dependencies

- `core.*` (engine, state, config, etc.)
- `integrations.*` (app_server, telegram, etc.)
- `surfaces.cli` (shared CLI utilities)
- Third-party web frameworks (FastAPI, starlette)

## Key Components

- `app.py`: Main FastAPI application factory
- `routes/`: API route handlers
- `middleware.py`: HTTP middleware (auth, base path, security headers)
- `schemas.py`: Request/response schemas
- `review.py`: Review workflow orchestration (moved from core/)
- `runner_manager.py`: Runner process lifecycle management
- `terminal_sessions.py`: PTY session management for TUI
- `static_assets.py`: Static asset management and caching

## Discoverability Entry Points

- PMA web/API surface: `routes/pma.py` (`/hub/pma/*`)
  - Chat/session lifecycle: `POST /hub/pma/chat`, `GET /hub/pma/active`, `GET /hub/pma/history`, `POST /hub/pma/interrupt|stop|new|reset|compact`, `GET /hub/pma/turns/{turn_id}/events`
  - Managed threads: `POST /hub/pma/threads`, `GET /hub/pma/threads`, `POST /hub/pma/threads/{managed_thread_id}/messages|resume|compact|archive|interrupt`, `GET /hub/pma/threads/{managed_thread_id}/turns`
  - PMA docs + context: `GET /hub/pma/docs`, `GET/PUT /hub/pma/docs/{name}`, `GET /hub/pma/docs/history/{name}`, `POST /hub/pma/context/snapshot`
  - FileBox-backed PMA files: `GET /hub/pma/files`, `POST /hub/pma/files/{box}`, `GET/DELETE /hub/pma/files/{box}/{filename}`, `DELETE /hub/pma/files/{box}`
  - Queue + dispatch + model/safety discovery: `GET /hub/pma/queue`, `GET /hub/pma/dispatches`, `POST /hub/pma/dispatches/{dispatch_id}/resolve`, `GET /hub/pma/agents`, `GET /hub/pma/agents/{agent}/models`, `GET /hub/pma/audit/recent`, `GET /hub/pma/safety/stats`
  - CLI companion: `car pma --help` plus `car pma thread|targets|docs|context --help` (`surfaces/cli/pma_cli.py`)
- Hub repo destination/settings management: `routes/hub_repos.py`
  - Destination source of truth for effective destination: `GET /hub/repos` (repo list includes `effective_destination` and related repo metadata)
  - Destination update: `POST /hub/repos/{repo_id}/destination`
  - Combined destination + worktree setup: `POST /hub/repos/{repo_id}/settings`
  - Worktree setup-only update: `POST /hub/repos/{repo_id}/worktree-setup`
  - CLI companion: `car hub destination --help` and `car hub destination show|set` (`surfaces/cli/commands/hub.py`)
- Setup docs for operators:
  - `docs/AGENT_SETUP_GUIDE.md`
  - `docs/configuration/destinations.md`
