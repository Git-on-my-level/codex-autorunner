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
