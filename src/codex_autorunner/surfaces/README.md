# Surfaces Package

This package contains surface-specific code for codex-autorunner. Surfaces are responsible for rendering state, collecting inputs, and providing ergonomics.

## Structure

- `web/`: FastAPI web UI, API routes, and web-specific workflows
- `cli/`: Command-line interface
- `discord/`: Discord `SurfacePort` facade and inbound event normalization
- `telegram/`: Telegram `SurfacePort` facade and inbound event normalization

Chat platform API clients remain in `adapters/` as outbound transport
adapters. Surface packages own the PMA-facing surface contract and translate
adapter-normalized inbound events into canonical engine commands.

## Architecture

Surfaces are Layer 3 (outermost). For the full layer definitions, dependency rules, and enforcement, see `docs/ARCHITECTURE_BOUNDARIES.md`.

Allowed dependencies: Surfaces may import from `core.*`, `adapters.*`, and other surface modules. Vendor SDK assumptions stay in adapters.
