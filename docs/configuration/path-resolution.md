# Path Resolution Rules

This document describes how codex-autorunner resolves paths in configuration files.

## Overview

All config paths follow consistent resolution rules to eliminate confusion about which paths are absolute vs relative, and when `~` expansion occurs.

## Relative Paths

All config paths without leading `/` or `~` are interpreted as **relative to the repo root**.

**Examples:**

```yaml
docs:
  todo: .codex-autorunner/TODO.md
  progress: .codex-autorunner/PROGRESS.md

log:
  path: .codex-autorunner/codex-autorunner.log
```

If the repo root is `/repo`, these resolve to:
- `docs.todo` → `/repo/.codex-autorunner/TODO.md`
- `docs.progress` → `/repo/.codex-autorunner/PROGRESS.md`
- `log.path` → `/repo/.codex-autorunner/codex-autorunner.log`

## Home Directory Expansion

Paths starting with `~` are expanded to the user's home directory.

**Examples:**

```yaml
app_server:
  state_root: ~/.codex-autorunner/workspaces

housekeeping:
  rules:
    - name: update_cache
      path: ~/.codex-autorunner/update_cache
```

These resolve to:
- `app_server.state_root` → `/home/user/.codex-autorunner/workspaces`
- `update_cache.path` → `/home/user/.codex-autorunner/update_cache`

## Absolute Paths

Paths starting with `/` are used as-is (absolute paths).

**Allowed locations:**
- `agents.<agent>.binary` — if you need to specify an absolute path to an agent binary
- `agents.<agent>.serve_command` — if you need to override the serve command with absolute paths

**Not allowed:**
- `docs.*` — must be relative to repo root
- `log.path` — must be relative to repo root
- `server_log.path` — must be relative to repo root
- `housekeeping.rules.*.path` — must be relative to repo root (or `~`-prefixed)

## Prohibited Patterns

### `..` Segments

Path segments containing `..` are rejected at config load time for security reasons.

**Invalid:**

```yaml
docs:
  todo: ../external/TODO.md  # Rejected at load time
```

**Error message:**

```
ConfigError: docs.todo must not contain '..' segments
```

### Paths Outside Repo Root

Relative paths that resolve outside the repo root are rejected.

**Invalid:**

```yaml
docs:
  todo: /etc/config  # Absolute paths not allowed for docs
```

**Valid:**

```yaml
docs:
  todo: .codex-autorunner/TODO.md  # Relative to repo root
```

### Empty Paths

Empty or whitespace-only paths are rejected.

**Invalid:**

```yaml
docs:
  todo: ""  # Rejected
  progress: "   "  # Rejected (whitespace only)
```

## Configuration Sections

### docs

All `docs.*` paths must be **relative to repo root** (no `~` or absolute paths allowed).

**Examples:**

```yaml
docs:
  todo: .codex-autorunner/TODO.md
  progress: .codex-autorunner/PROGRESS.md
  opinions: .codex-autorunner/OPINIONS.md
  spec: .codex-autorunner/SPEC.md
  summary: .codex-autorunner/SUMMARY.md
  snapshot: .codex-autorunner/SNAPSHOT.md
  snapshot_state: .codex-autorunner/snapshot_state.json
```

### log and server_log

Log paths must be **relative to repo root**.

**Examples:**

```yaml
log:
  path: .codex-autorunner/codex-autorunner.log

server_log:
  path: .codex-autorunner/codex-server.log
```

### app_server.state_root

The app server state root can be either:
- Relative to repo root: `.codex-autorunner/workspaces`
- Home directory expansion: `~/.codex-autorunner/workspaces`

**Examples:**

```yaml
app_server:
  state_root: ~/.codex-autorunner/workspaces
```

### static_assets.cache_root

The static assets cache root can be either:
- Relative to repo root: `.codex-autorunner/static-cache`
- Home directory expansion: `~/.codex-autorunner/static-cache`

**Examples:**

```yaml
static_assets:
  cache_root: .codex-autorunner/static-cache
```

### housekeeping.rules.*.path

Housekeeping rule paths can be either:
- Relative to repo root: `.codex-autorunner/runs`
- Home directory expansion: `~/.codex-autorunner/update_cache`

**Examples:**

```yaml
housekeeping:
  rules:
    - name: run_logs
      kind: directory
      path: .codex-autorunner/runs

    - name: update_cache
      kind: directory
      path: ~/.codex-autorunner/update_cache
```

### agents

Agent binaries can be:
- A simple command name: `codex` (resolved via PATH)
- A relative path: `./bin/codex` (relative to repo root)
- An absolute path: `/usr/local/bin/codex`
- Home directory expansion: `~/bin/codex`

**Examples:**

```yaml
agents:
  codex:
    binary: codex

  opencode:
    binary: opencode
    serve_command:
      - /absolute/path/to/opencode
      - serve
```

## Error Messages

When a path is invalid, the error message includes:
- The config key (e.g., `docs.todo`)
- The invalid path value
- The resolved path (for `~` expansion)
- A helpful hint for fixing it

**Example:**

```
ConfigError: docs.todo must not contain '..' segments: '../external/TODO.md'
```

## Migration Notes

If you're upgrading from an older version, check your config for:

1. **Absolute paths in docs/log sections** — convert to repo-root relative paths
2. **`..` segments** — use explicit paths or copy files to desired location
3. **Missing path validation** — paths that failed at runtime now fail at load time

## Security Considerations

The `..` segment rejection prevents directory traversal attacks. While codex-autorunner typically runs in trusted environments, this provides defense-in-depth protection.

## Windows Compatibility

On Windows, the `~` expansion uses the user's home directory (e.g., `C:\Users\username\`). Path separators (`/` or `\`) are normalized internally. However, the recommended approach is to use forward slashes (`/`) in config files for consistency.
