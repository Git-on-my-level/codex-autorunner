# Telegram Surface

The Telegram surface provides Telegram bot-specific user interface code for Codex AutoRunner.

## Responsibilities

- Telegram-specific command handlers and interactions
- Telegram message formatting and rendering
- Telegram-specific UI components (buttons, menus)
- Telegram workflow orchestration (when different from general adapter code)

## Allowed Dependencies

- **CAN import from**: `core/`, `integrations/`, `agents/`, `tickets/`, `workspace/`
- **MUST NOT import from**: Other surface packages (`cli/`, `web/routes/`)
- **CAN be imported by**: `integrations/telegram/` adapter

## Architecture

Telegram surface coordinates with the Telegram adapter:
```
[ Telegram Surface ] → [ Telegram Adapter ] → [ Control Plane ] → [ Engine ]
```

The actual Telegram adapter implementation lives in `integrations/telegram/`. This surface package contains Telegram-specific UI and interaction code.

## Current Status

This package is currently a placeholder. All Telegram-specific code is in:
- `src/codex_autorunner/integrations/telegram/` - Telegram adapter implementation
- `src/codex_autorunner/surfaces/cli/cli.py` - Telegram CLI commands

Future Telegram-specific UI components (beyond adapter code) can be added here.
