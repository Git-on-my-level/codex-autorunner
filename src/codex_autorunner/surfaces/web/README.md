# Web Surface

The web surface provides the FastAPI server and web UI for Codex AutoRunner.

## Responsibilities

- FastAPI application and server lifecycle
- HTTP routes for web API endpoints
- Static asset serving (HTML, CSS, JS)
- WebSocket connections for terminal sessions
- Request middleware and authentication
- Web-specific schemas and data models

## Allowed Dependencies

- **CAN import from**: `core/`, `integrations/`, `agents/`, `tickets/`, `workspace/`
- **MUST NOT import from**: Other surface packages (`cli/`, `telegram/`)
- **CAN be imported by**: `server.py`, other web surface modules

## Architecture

Web surface sits at the top of the layer hierarchy:
```
[ Web Surface ] → [ Adapters ] → [ Control Plane ] → [ Engine ]
```

It orchestrates adapters and coordinates with the engine to provide a user-facing API.

## Key Modules

- `app.py`: FastAPI application factory and startup/shutdown logic
- `routes/`: HTTP route handlers organized by feature
- `middleware.py`: Request/response middleware
- `schemas.py`: Pydantic models for API requests/responses
- `static_assets.py`: Static file serving and HTML rendering
- `pty_session.py`: PTY terminal session management
- `review.py`: Review workflow orchestration (web-triggered)

## Integration Notes

- Uses OpenCode and app-server adapters via `integrations/`
- Coordinates with engine via `core/engine.py`
- Stores runtime state in `.codex-autorunner/` directory
