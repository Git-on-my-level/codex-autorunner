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

Use `/flow ...` to control ticket flow for a bound workspace, or view hub overview
for `status`/`runs` in PMA or unbound contexts.

Note: Telegram currently exposes the superset of flow controls across chat surfaces.

Command reference:
- `/flow` or `/flow help`
- `/flow status [run_id]`
- `/flow runs [N]`
- `/flow issue <issue#|url>`
- `/flow plan <text>`
- `/flow resume [run_id]`
- `/flow stop [run_id]`
- `/flow recover [run_id]`
- `/flow archive [run_id] [--force]`
- `/flow reply <message>`

Aliases:
- `/reply` (legacy alias for `/flow reply`)

Examples:
- Seed ISSUE.md from GitHub issue: `/flow issue 123` (or issue URL)
- Seed ISSUE.md from plan text: `/flow plan <text>`
- Start new ticket-flow runs from `/pma`, then use `/flow status` for controls.
- Status + buttons: `/flow status` (use Resume/Stop/Recover/Archive/Refresh buttons)
- Reply to a pause: `/flow reply <message>`, then `/flow resume`
