# Surfaces Package

This package contains surface-specific code for codex-autorunner. Surfaces are responsible for rendering state, collecting inputs, and providing ergonomics.

## Structure

- `web/`: FastAPI web UI, API routes, and web-specific workflows
- `cli/`: Command-line interface

> **Note:** Chat platforms (Discord, Telegram) have no separate surface layer.
> The integration IS the surface — all code lives directly in
> `integrations/discord/` and `integrations/telegram/`. Unlike Web (which has
> distinct routes, middleware, and static assets), chat platforms are thin
> adapters over their respective APIs, so an extra surface indirection adds
> complexity without benefit.

## Architecture

Surfaces are Layer 3 (outermost). For the full layer definitions, dependency rules, and enforcement, see `docs/ARCHITECTURE_BOUNDARIES.md`.

Allowed dependencies: Surfaces may import from `core.*`, `integrations.*`, and other surface modules. Vendor SDK assumptions stay in integrations.
