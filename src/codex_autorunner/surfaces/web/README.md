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
