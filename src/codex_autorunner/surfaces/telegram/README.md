# Telegram Surface

Telegram bot and adapters.

## Responsibilities

- Telegram bot interface
- Message routing and handlers
- Telegram-specific ergonomics
- Telegram state management

## Allowed Dependencies

- `core.*` (engine, config, state, etc.)
- `integrations.*` (telegram, app_server, etc.)
- Third-party Telegram libraries (python-telegram-bot, etc.)

## Key Components

- Telegram bot entry points and handlers are in `integrations/telegram/`
- This surface package may contain Telegram-specific UI/rendering code if needed in the future

## Ticket Flow Commands

Use `/flow ...` to control the ticket flow for a bound workspace.

Command reference:
- `/flow` or `/flow help`
- `/flow status [run_id]`
- `/flow runs [N]`
- `/flow bootstrap [--force-new]`
- `/flow issue <issue#|url>`
- `/flow plan <text>`
- `/flow resume [run_id]`
- `/flow stop [run_id]`
- `/flow recover [run_id]`
- `/flow restart`
- `/flow archive [run_id] [--force]`
- `/flow-archive [run_id] [--force]` (alias for `/flow archive`)
- `/flow-interrupt` (alias for `/interrupt`)
- `/flow reply <message>`

Aliases:
- `/flow_status` (status only)
- `/reply` (legacy alias for `/flow reply`)

Examples:
- Bootstrap from GitHub issue:
  - `/flow bootstrap` then send `https://github.com/org/repo/issues/123`
  - `/flow issue 123`
- Bootstrap from plan text: `/flow plan <text>`
- Status + buttons: `/flow status` (use Resume/Stop/Recover/Restart/Archive/Refresh buttons)
- Reply to a pause: `/flow reply <message>`, then `/flow resume`
