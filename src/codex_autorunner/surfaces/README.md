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

Surfaces are the outermost layer in the architecture map:

```
[ Surfaces ] → [ Adapters ] → [ Control Plane ] → [ Engine ]
```

**Responsibilities:**
- Render state
- Collect inputs
- Support reconnects
- Provide ergonomics (logs, terminal, dashboards)

**Non-responsibilities:**
- Do not become state owners; never be the only place truth lives

## Allowed Dependencies

Surfaces MAY import from:
- `core.*` (engine and control-plane primitives)
- `integrations.*` (adapters and backend orchestration)
- Other surface modules

Surfaces MUST NOT import from:
- External vendor SDK assumptions should be isolated to integrations

## One-way Dependency Rule

Dependencies must flow in one direction:

```
Surfaces → Adapters → Control Plane → Engine
```

Never reverse dependencies (e.g., core importing from surfaces).
