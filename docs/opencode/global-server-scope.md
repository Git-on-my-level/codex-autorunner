# OpenCode Global Server Scope

CAR supports two OpenCode server scopes via `opencode.server_scope`:

- `workspace` (default): one `opencode serve` process per workspace.
- `global`: one shared `opencode serve` process reused across workspaces.

## Configuration

```yaml
opencode:
  server_scope: global
```

## When to Use `global`

Use `global` when:
- you want fewer background processes
- you run many repos/workspaces on the same machine
- you are comfortable sharing one OpenCode server lifecycle across those workspaces

Keep `workspace` when:
- you prefer stricter process isolation per workspace
- you want lifecycle boundaries to follow workspace boundaries

## Lifecycle Notes

- In `global` scope, CAR reuses a single supervisor handle and process for multiple workspaces.
- On shutdown/idle prune, CAR makes a best-effort `POST /global/dispose` call before terminating the server, so cached instances are released first.
