# Migration: workspace → contextspace

## Overview

This migration moves repo-local shared documents from `.codex-autorunner/workspace/` to `.codex-autorunner/contextspace/`. This is a breaking change for existing repos.

## Why the rename?

The `contextspace` name better reflects the purpose of these documents: they provide context to agents during ticket execution. The term "workspace" was ambiguous and could be confused with the actual working directory.

## Running the migration

### Automatic migration (recommended)

Run the provided migration script:

```bash
./scripts/migrate-workspace-to-contextspace.sh
```

The script will:
- Detect the repo root (or use `--repo <path>` if you specify it)
- Create `.codex-autorunner/contextspace/` if needed
- Move files from workspace to contextspace
- Print a summary of what was moved
- Clean up the old directory if it becomes empty

### Dry run

To see what would happen without making changes:

```bash
./scripts/migrate-workspace-to-contextspace.sh --dry-run
```

### Migrating a specific repo

```bash
./scripts/migrate-workspace-to-contextspace.sh --repo /path/to/repo
```

## Safety checks

The script is designed to be safe and idempotent:

| Scenario | Behavior |
|----------|----------|
| Old dir missing | Prints "nothing to do" and exits |
| Old empty, new present | Removes old and exits |
| New empty, old has content | Moves old → new |
| Both non-empty | **Fails** with instructions |

The script will **never** silently overwrite or merge files.

## Manual verification

After migration, verify the structure:

```bash
ls -la .codex-autorunner/contextspace/
```

You should see documents like:
- `active_context.md` (optional, auto-created)
- `decisions.md` (optional)
- `spec.md` (optional)

## Config implications

If you have custom configs that override the default contextspace paths, update them:

```yaml
# Old path (no longer used)
docs:
  active_context: .codex-autorunner/workspace/active_context.md
  decisions: .codex-autorunner/workspace/decisions.md
  spec: .codex-autorunner/workspace/spec.md

# New path (use this instead)
docs:
  active_context: .codex-autorunner/contextspace/active_context.md
  decisions: .codex-autorunner/contextspace/decisions.md
  spec: .codex-autorunner/contextspace/spec.md
```

## Rollback

If you need to rollback (e.g., to test the old workspace behavior):

```bash
# Ensure contextspace exists
mv .codex-autorunner/contextspace .codex-autorunner/workspace
```

However, this is not recommended as the workspace path is deprecated.

## Troubleshooting

### "Both workspace and contextspace directories exist and contain files"

This means both directories have content. The script refuses to migrate to avoid data loss.

**Resolution:** Manually inspect and resolve:
```bash
# Check contents
ls -la .codex-autorunner/workspace/
ls -la .codex-autorunner/contextspace/

# If contextspace is correct, remove workspace
rm -rf .codex-autorunner/workspace/

# Or move important files from workspace to contextspace
mv .codex-autorunner/workspace/* .codex-autorunner/contextspace/
rmdir .codex-autorunner/workspace/
```

### "Nothing to do: workspace directory not found"

The migration may have already been done, or you're using a fresh repo. No action needed.

## Version compatibility

- **Before:** CAR uses `.codex-autorunner/workspace/`
- **After:** CAR uses `.codex-autorunner/contextspace/`
- The runtime will only read from the new path; old configs pointing to workspace will need updating.

## Additional resources

- [CAR Architecture](../car_constitution/20_ARCHITECTURE_MAP.md)
- [Agent Cheatsheet](../car_constitution/61_AGENT_CHEATSHEET.md)
- [Configuration](../configuration/index.md)
