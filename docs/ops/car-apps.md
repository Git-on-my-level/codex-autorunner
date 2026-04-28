# CAR Apps Operator Guide

Operations guide for installing, configuring, and managing CAR Apps.

## Enabling apps

Add an `apps` section to your hub configuration (`codex-autorunner.yml`):

```yaml
apps:
  enabled: true
  repos:
    - id: my-org
      url: https://github.com/my-org/car-apps
      trusted: true
      default_ref: main
```

When `apps.enabled` is `false`, all `car apps` commands except basic help and
config diagnostics refuse app operations with a clear message.

## Configuring app source repos

App repos are Git repositories that contain one or more app bundle directories.
Each bundle is a directory with a `car-app.yaml` file.

The `apps.repos` config is separate from `templates.repos` because executable
bundles have different trust and lifecycle semantics than static Markdown
templates.

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Unique identifier for this repo |
| `url` | yes | Git repository URL |
| `trusted` | no | Whether to allow install/run without approval (default: `false`) |
| `default_ref` | no | Default Git ref (default: `main`) |

## Installing apps

```bash
# Install from a source reference
car apps install my-org:apps/my-workflow

# Force reinstall after an update
car apps install my-org:apps/my-workflow --force
```

Installation:

1. Fetches the app repo through a hub-scoped mirror.
2. Validates the `car-app.yaml` manifest.
3. Materializes bundle files under `.codex-autorunner/apps/<id>/bundle/`.
4. Creates `app.lock.json` with provenance and integrity hashes.
5. Creates `state/`, `artifacts/`, and `logs/` directories.

### Idempotent install

Installing the same source reference twice without changes is a no-op. The
lock file, bundle hash, and commit SHA must all match.

### Conflict handling

Installing a different version of an already-installed app without `--force`
produces a clear error. Use `--force` to replace the existing installation.

### Bundle integrity

Every tool run verifies the installed bundle SHA against `app.lock.json`. If
the bundle has been tampered with or corrupted, tool execution is refused until
the app is reinstalled.

## Applying apps

```bash
# Apply with inputs
car apps apply my-org:apps/my-workflow --set goal="Fix the bug"

# Apply using an installed app id
car apps apply my-org.my-workflow --set goal="Fix the bug"

# Explicit ticket index
car apps apply my-org.my-workflow --at 5 --set goal="Fix the bug"
```

Applying an app:

1. Installs the app first if not already installed (when using a source ref).
2. Creates an entrypoint ticket from `entrypoint.template`.
3. Injects app provenance into ticket frontmatter (`app`, `app_version`,
   `app_source`, `app_commit`, `app_manifest_sha`, `app_bundle_sha`).
4. Writes apply inputs to `state/apply-inputs.json`.
5. Preserves all existing ticket-flow rules.

## Running tools

```bash
# Run a tool
car apps run my-org.my-workflow record-state -- "hello world"

# With timeout override
car apps run my-org.my-workflow render-summary --timeout 30
```

Tool execution:

1. Loads and validates `app.lock.json`.
2. Verifies bundle integrity against the lock.
3. Resolves the tool from the manifest.
4. Builds argv from manifest plus extra args after `--`.
5. Creates missing runtime directories.
6. Sets `CAR_*` environment variables.
7. Runs the process with `shell=False`.
8. Captures stdout/stderr to bounded log files.
9. Registers declared outputs as flow artifacts when a flow run is active.

## Monitoring hooks

Hooks fire automatically during ticket-flow execution. You can observe them
through flow events:

- `APP_HOOK_STARTED`: Hook execution began.
- `APP_HOOK_RESULT`: Hook execution finished (with exit code, duration, error).

Hook failures follow the manifest-declared policy:

| Policy | Effect on flow |
|--------|---------------|
| `warn` | Log and continue |
| `pause` | Pause the flow run |
| `fail` | Fail the flow run |

## Managing artifacts

```bash
# List all artifacts for an app
car apps artifacts my-org.my-workflow

# Filter by flow run
car apps artifacts my-org.my-workflow --run-id run-123
```

Artifacts are produced by tool scripts in the `artifacts/` directory. They are:

- Registered against flow runs when `CAR_FLOW_RUN_ID` is available.
- Included in flow archive retention.
- Eligible for Discord/Telegram notification attachments.
- Listed via `car apps artifacts` and the web/API.

## App state

App-owned state lives under `.codex-autorunner/apps/<id>/state/`. Common
patterns:

- JSONL append-only logs for records.
- JSON files for configuration or accumulated state.
- SQLite databases for complex app-local queries.

App state is **not** part of the core CAR database schema. Apps manage their
own state format and lifecycle.

## Trust and security

| Scenario | Behavior |
|----------|----------|
| Trusted repo, install | Allowed, provenance recorded |
| Untrusted repo, install | Requires explicit approval |
| Untrusted app, hooks | Disabled until explicitly approved |
| Bundle hash mismatch | Tool execution refused |
| Manifest path escape | Rejected at install and run time |
| `shell=True` | Never used |

## Uninstalling / cleanup

Remove the installed app directory:

```bash
rm -rf .codex-autorunner/apps/<app-id>/
```

There is no uninstall command in v1. Removing the directory is
sufficient because apps do not modify core database schemas.

## Troubleshooting

### "Installed app bundle does not match app.lock.json"

The installed bundle has been modified or corrupted. Reinstall:

```bash
car apps install my-org:apps/my-workflow --force
```

### "App already installed" conflict

A different version is installed. Reinstall with `--force` or use the installed
app id directly.

### "apps.enabled is false"

Enable apps in your hub configuration:

```yaml
apps:
  enabled: true
```

### Hooks not firing

- Check that the app is installed and trusted.
- Verify the `when` selector matches the ticket frontmatter or flow status.
- Check tool logs under `.codex-autorunner/apps/<id>/logs/`.
