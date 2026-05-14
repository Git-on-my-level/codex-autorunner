# OpenCode Global Server Scope

CAR supports two OpenCode server scopes via `opencode.server_scope`:

- `global` (default): one shared `opencode serve` process reused across workspaces.
- `workspace`: one `opencode serve` process per workspace.

The default retention posture is intentionally conservative for long-lived CAR
workloads:

- `server_scope: global`
- `max_handles: 1`
- `idle_ttl_seconds: 900`

This keeps CAR to one governed OpenCode server per hub/runtime profile by
default. Move to `workspace` when you intentionally want per-workspace process
isolation.

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

Use `workspace` when:
- you prefer stricter process isolation per workspace
- you want lifecycle boundaries to follow workspace boundaries
- you want the default pressure policy to prune idle servers more aggressively

## Lifecycle Notes

- In `global` scope, CAR reuses a single supervisor handle and process for multiple workspaces.
- On shutdown/idle prune, CAR makes a best-effort `POST /global/dispose` call before terminating the server, so cached instances are released first.

## Tuning

For workloads that benefit from workspace isolation, opt in explicitly:

```yaml
opencode:
  server_scope: workspace
  max_handles: 4
  idle_ttl_seconds: 900
```
