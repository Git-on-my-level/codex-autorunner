# CLI Surface

The CLI surface provides the Typer-based command-line interface for Codex AutoRunner.

## Responsibilities

- Command-line argument parsing and validation
- CLI command implementations (init, run, serve, hub, etc.)
- User-facing error messages and help text
- Configuration file management and validation
- Process spawning and lifecycle management
- Terminal output formatting and logging

## Allowed Dependencies

- **CAN import from**: `core/`, `integrations/`, `agents/`, `tickets/`, `workspace/`, `web/` (for hub app creation)
- **MUST NOT import from**: Other surface packages (`telegram/`, `routes/`)
- **CAN be imported by**: Entry point script

## Architecture

CLI surface sits at the top of the layer hierarchy:
```
[ CLI Surface ] → [ Adapters ] → [ Control Plane ] → [ Engine ]
```

It provides a command-line interface to interact with the engine and adapters.

## Key Modules

- `cli.py`: Main Typer app and command definitions
- Commands organized by sub-app:
  - `app`: Main repo-level commands (init, run, once, status, log, etc.)
  - `hub_app`: Hub-level commands (serve, scan, create)
  - `telegram_app`: Telegram bot commands

## Integration Notes

- Uses engine via `core/engine.py`
- Uses adapters via `integrations/` (telegram, agents, etc.)
- Uses web app via `web/app.py` for hub server
- Entry point: `python -m codex_autorunner.surfaces.cli`
