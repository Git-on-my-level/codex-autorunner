# Surfaces Package

This package contains surface-specific code for codex-autorunner. Surfaces are responsible for rendering state, collecting inputs, and providing ergonomics.

## Structure

- `web/`: FastAPI web UI, API routes, and web-specific workflows
- `cli/`: Command-line interface

Transport adapters remain in `adapters/`. Surface packages own entrypoint-specific UI and routing code; core port contracts live under `core/ports/`.

## Architecture

Surfaces are Layer 3 (outermost). For the full layer definitions, dependency rules, and enforcement, see `docs/ARCHITECTURE_BOUNDARIES.md`.

Allowed dependencies: Surfaces may import from `core.*`, `adapters.*`, and other surface modules. Vendor SDK assumptions stay in adapters.
