# CLI Surface

Command-line interface for codex-autorunner.

## Responsibilities

- Provide command-line interface
- Parse and validate CLI arguments
- Execute commands and display results
- Handle CLI-specific error messaging

## Operator Habit

- In unfamiliar command areas, run `car <command-group> --help` first.
- Use command help examples as canonical usage before scripting.

## Allowed Dependencies

- `core.*` (engine, config, state, etc.)
- `integrations.*` (as needed)
- Third-party CLI libraries (click, argparse, etc.)

## Key Components

- `cli.py`: Main CLI entry point and command handlers
- `codex_cli.py`: Codex-specific CLI utilities
