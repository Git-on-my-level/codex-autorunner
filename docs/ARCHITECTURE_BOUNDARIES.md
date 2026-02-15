# Architecture Boundaries

This document defines the architecture layers and dependency rules for the codex-autorunner codebase.

## Layer Definitions

The codebase is organized into four layers, from most stable (left) to most volatile (right):

```
[ Engine ] → [ Control Plane ] → [ Adapters ] → [ Surfaces ]
```

### Engine (Layer 0)

**Responsibilities:**
- Run lifecycle + state transitions
- Scheduling, locks, queues
- Deterministic semantics
- Protocol-agnostic abstractions

**Module prefixes:**
- `codex_autorunner.core.flows*` - Flow engine and state machine
- `codex_autorunner.core.ports*` - Interface definitions and protocols

**Non-responsibilities:**
- No UI concepts
- No transport/protocol coupling
- No vendor SDK assumptions

### Control Plane (Layer 1)

**Responsibilities:**
- Canonical state and artifacts under `.codex-autorunner/`
- Plans, snapshots, outputs, run metadata
- Durable bridge between humans, agents, and engine

**Module prefixes:**
- `codex_autorunner.core*` (excluding flows/ports) - Core utilities, config, state
- `codex_autorunner.contextspace*` - Context space document management
- `codex_autorunner.tickets*` - Ticket processing and management

### Adapters (Layer 2)

**Responsibilities:**
- Translate external events/requests into engine commands
- Normalize streaming/logging into canonical run artifacts
- Tolerate retries, restarts, partial failures

**Module prefixes:**
- `codex_autorunner.integrations*` - External integrations (Telegram, App Server, templates)
- `codex_autorunner.agents*` - Agent implementations (codex, opencode)

**Non-responsibilities:**
- Avoid owning business logic; keep logic in engine/control plane

### Surfaces (Layer 3)

**Responsibilities:**
- Render state; collect inputs; support reconnects
- Provide ergonomics (logs, terminal, dashboards)

**Module prefixes:**
- `codex_autorunner.surfaces*` - Web UI, CLI, Telegram surface

**Non-responsibilities:**
- Do not become state owners; never be the only place truth lives

## Allowed Dependencies

Dependencies flow **one direction only**: from right (volatile) to left (stable).

```
Surfaces → Adapters → Control Plane → Engine
```

| Layer | Can import from |
|-------|-----------------|
| Engine | stdlib, external packages only |
| Control Plane | Engine, stdlib, external packages |
| Adapters | Control Plane, Engine, stdlib, external packages |
| Surfaces | Adapters, Control Plane, Engine, stdlib, external packages |

**Forbidden:**
- Engine importing Control Plane, Adapters, or Surfaces
- Control Plane importing Adapters or Surfaces
- Adapters importing Surfaces

## Enforcement

Architecture boundaries are enforced by an automated test:

```bash
python -m pytest tests/test_architecture_boundaries.py -q
```

This test uses AST parsing to statically analyze all imports within `src/codex_autorunner/` and reports violations with clear error messages.

### Test Failure Example

```
Architecture boundary violations detected:

Allowed dependency direction: Surfaces -> Adapters -> Control Plane -> Engine

Reverse dependencies are forbidden.
--------------------------------------------------------------------------------
VIOLATION: src/codex_autorunner/core/utils.py
  Layer: CONTROL_PLANE
  Forbidden import: codex_autorunner.surfaces.web (SURFACES)
  Allowed direction: CONTROL_PLANE -> SURFACES
--------------------------------------------------------------------------------
```

## Adding New Modules

### Adding a new Adapter

1. Create the module under `src/codex_autorunner/integrations/` or `src/codex_autorunner/agents/`
2. Import from Control Plane or Engine layers as needed
3. Do NOT import from Surfaces
4. Run the boundary test to verify

### Adding a new Surface

1. Create the module under `src/codex_autorunner/surfaces/`
2. Import from any layer below (Adapters, Control Plane, Engine)
3. Run the boundary test to verify

### Adding shared utilities

If code is needed by multiple layers:
1. Place it in the **lowest layer** that needs it
2. If needed by Engine and above, place in Engine (e.g., `core/ports/`)
3. If needed by Control Plane and above (but not Engine), place in Control Plane

## Shim Modules

In rare cases, a reverse dependency may be unavoidable (e.g., plugin registration, legacy compatibility). Use a shim module:

1. Name the file `*_shim.py` or create a `*_shim/` package
2. Add a comment header with `ARCHITECTURE_SHIM:` explaining the exception
3. Keep the shim minimal and document why it's necessary

Example:

```python
"""Configuration shim for adapter registration.

ARCHITECTURE_SHIM: This module allows adapters to register themselves
with the control plane at import time. This is a temporary measure
until we migrate to explicit registration in config.
"""

from codex_autorunner.core.config import register_adapter
```

## Related Documentation

- Architecture Map: `docs/car_constitution/20_ARCHITECTURE_MAP.md`
- Engineering Standards: `docs/car_constitution/30_ENGINEERING_STANDARDS.md`
