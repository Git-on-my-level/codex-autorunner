# CAR Plugin API

This document describes the stable public API surface for external plugins.

## Scope

CAR supports plugin loading via Python packaging **entry points**.

Currently supported plugin type:

- **Agent backends**: add a new agent implementation (harness + supervisor).

## Versioning

Plugins MUST declare compatibility with the current plugin API version:

- `codex_autorunner.api.CAR_PLUGIN_API_VERSION`

CAR will skip plugins whose declared `plugin_api_version` does not match.

## Agent backend entry point

Entry point group:

- `codex_autorunner.api.CAR_AGENT_ENTRYPOINT_GROUP`
- (currently: `codex_autorunner.agent_backends`)

A plugin package should expose an `AgentDescriptor` object:

```python
from codex_autorunner.api import AgentDescriptor, AgentHarness, CAR_PLUGIN_API_VERSION

def _make(ctx: object) -> AgentHarness:
    raise NotImplementedError

AGENT_BACKEND = AgentDescriptor(
    id="myagent",
    name="My Agent",
    capabilities=frozenset(["threads", "turns"]),
    make_harness=_make,
    plugin_api_version=CAR_PLUGIN_API_VERSION,
)
```

and declare it in `pyproject.toml`:

```toml
[project.entry-points."codex_autorunner.agent_backends"]
myagent = "my_package.my_agent_plugin:AGENT_BACKEND"
```

Notes:

- Plugin ids are normalized to lowercase.
- Plugins cannot override built-in agent ids.
- Plugins SHOULD avoid import-time side effects; do heavy initialization inside `make_harness`.
