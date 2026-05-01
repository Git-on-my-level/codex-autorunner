# CAR Apps

CAR Apps are bundled workflow extensions that ship ticket templates, executable
tools, lifecycle hooks, artifact declarations, and app-owned state in a single
directory. Apps extend CAR without adding domain-specific behavior to the core
database, CLI, or chat integrations.

## What CAR Apps are

A CAR App is a directory containing a `car-app.yaml` manifest plus optional
templates, scripts, and documentation. Apps:

- Contain ticket templates and executable helper scripts.
- Declare tools that agents invoke through `car apps run`.
- Declare hooks that fire at specific lifecycle points.
- Own their own state files and artifact output.
- Are installed from configured Git repositories and locked to a specific
  commit with integrity verification.

## How apps differ from ticket templates and skills

| Aspect | Ticket templates | CAR Apps | Skills |
|--------|-----------------|----------|--------|
| Format | Static Markdown | Manifest + scripts + templates | Agent instructions |
| Executable code | None | Declared tools run as subprocesses | None |
| State | Ticket frontmatter only | App-owned state files under `.codex-autorunner/apps/<id>/state/` | N/A |
| Lifecycle hooks | None | `after_ticket_done`, `after_flow_terminal`, `before_chat_wrapup` | N/A |
| Artifacts | None | Declared outputs registered with flow runs | N/A |
| Source | Template repos | App repos (separate config) | Agent config |

CAR Apps **are not** a replacement for ticket templates. An app may contain
templates, but `car templates` remains the static Markdown-only primitive.
Templates do not gain executable semantics.

## Bundle layout

Only `car-app.yaml` is required. A typical bundle:

```
apps/<slug>/
  car-app.yaml          # Required manifest
  README.md             # Optional documentation
  templates/
    entry.md            # Entrypoint ticket template
  scripts/
    record_state.py     # Tool script
    render_card.py      # Tool script producing artifacts
  docs/
    operator-guide.md   # Optional operator documentation
```

## Installed app layout

Installing an app creates a repo-local directory under
`.codex-autorunner/apps/<app-id>/`:

```
.codex-autorunner/apps/<app-id>/
  app.lock.json         # Installation metadata and integrity hashes
  bundle/               # Materialized copy of the bundle files
    car-app.yaml
    templates/
    scripts/
  state/                # App-owned state (JSON, JSONL, etc.)
  artifacts/            # Tool-produced output files
  logs/                 # Execution logs
```

App-specific data belongs in app-owned state files under `state/`, **not** in
core database schemas. CAR core stores only installation metadata, hook/tool
execution events, and artifact references.

## Quick start

### 1. Use the default blessed app repo

Fresh hub configs enable apps and point the `blessed` app repo id at the
blessed CAR app catalog:

```yaml
apps:
  enabled: true
  repos:
    - id: blessed
      url: https://github.com/Git-on-my-level/blessed-car-apps
      trusted: true
      default_ref: main
```

The template catalog remains separate:

```yaml
templates:
  repos:
    - id: blessed
      url: https://github.com/Git-on-my-level/car-ticket-templates
      trusted: true
      default_ref: main
```

This split is intentional. Template repos contain static Markdown. App repos
can contain executable tools and hooks.

### 2. Add an organization app source repo

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

### 3. List available apps

```bash
car apps list
```

### 4. Use AutoOptimize

When a user asks PMA or a CLI operator to "use the AutoOptimize CAR app", the
default path is:

```bash
car apps list --repo /path/to/repo
car apps show blessed:apps/autooptimize --repo /path/to/repo
car apps install blessed:apps/autooptimize --repo /path/to/repo
car apps apply blessed.autooptimize --repo /path/to/repo --set goal="Improve performance"
```

After installation, PMA can inspect tools and run the app-owned commands:

```bash
car apps tools blessed.autooptimize --repo /path/to/repo
car apps run blessed.autooptimize status --repo /path/to/repo
```

### 5. Install another app

```bash
car apps install my-org:apps/my-workflow
```

### 6. Apply (create an entrypoint ticket)

```bash
car apps apply my-org:apps/my-workflow --set goal="Improve performance"
```

Or using an installed app id:

```bash
car apps apply my-org.my-workflow --set goal="Improve performance"
```

Or apply a named template declared by the manifest:

```bash
car apps apply my-org.my-workflow --template iteration --set goal="Improve performance"
```

### 7. Run a tool

```bash
car apps run my-org.my-workflow record-state -- "hello world"
```

### 8. List artifacts

```bash
car apps artifacts my-org.my-workflow
```

## Trust model and security

- **Trusted repos** may be installed and run without additional approval, but
  provenance is always recorded in `app.lock.json`.
- **Untrusted repos** may be listed, shown, and installed so operators can
  inspect the locked bundle and provenance. Tool and hook execution is refused
  for untrusted installed apps. Reinstall from a trusted app repo before
  running tools.
- No app tool may run from a source ref that has not been installed and locked.
- No hook may run a tool when the installed bundle hash differs from
  `app.lock.json`.
- Tool execution always uses `shell=False`. Manifest paths are validated at
  install time and again before every run.
- Outputs must remain under app artifact/state/log directories unless the
  manifest explicitly declares a broader allowed path.
- Tool commands must use an argv array; shell strings are not accepted.
- Absolute paths, `..`, backslash separators, symlink escapes, and empty path
  segments are all rejected in manifest paths.

## Hook points

v1 supports three hook points:

| Hook point | When it fires |
|------------|---------------|
| `after_ticket_done` | After a ticket transitions to `done: true` |
| `after_flow_terminal` | After a ticket-flow run reaches `completed`, `failed`, or `stopped` |
| `before_chat_wrapup` | Before Discord/Telegram sends the final wrap-up message |

Hooks dispatch manifest-declared tools through the app tool runner. They do not
execute raw shell snippets. Hook failures are controlled by the `failure` field:
`warn` (default), `pause`, or `fail`.

## Tool runner environment variables

Every tool invocation receives these environment variables:

| Variable | Description |
|----------|-------------|
| `CAR_REPO_ROOT` | Absolute path to the target repo |
| `CAR_WORKSPACE_ROOT` | Absolute workspace root (same as repo root unless overridden) |
| `CAR_APP_ID` | Installed app id |
| `CAR_APP_VERSION` | Installed app version |
| `CAR_APP_ROOT` | Absolute `.codex-autorunner/apps/<app-id>/` |
| `CAR_APP_BUNDLE_DIR` | Absolute `.codex-autorunner/apps/<app-id>/bundle/` |
| `CAR_APP_STATE_DIR` | Absolute `.codex-autorunner/apps/<app-id>/state/` |
| `CAR_APP_ARTIFACT_DIR` | Absolute `.codex-autorunner/apps/<app-id>/artifacts/` |
| `CAR_APP_LOG_DIR` | Absolute `.codex-autorunner/apps/<app-id>/logs/` |
| `CAR_FLOW_RUN_ID` | Flow run id (when available) |
| `CAR_TICKET_PATH` | Ticket file path (when available) |
| `CAR_TICKET_ID` | Ticket id (when available) |
| `CAR_HOOK_POINT` | Hook point name (when invoked by hook) |

## Authoring guidelines for app scripts

1. Use `CAR_APP_STATE_DIR` and `CAR_APP_ARTIFACT_DIR` for all output.
2. Read inputs from `sys.argv` or environment variables.
3. Write structured output (JSON, JSONL, Markdown) to the state or artifact
   directory.
4. Exit with code 0 on success, non-zero on failure.
5. Keep tool scripts small and focused. Each tool should do one thing.
6. Do not depend on files outside the bundle or app runtime directories.
7. Avoid interactive input; tools run non-interactively.
8. Honor `CAR_HOOK_POINT` when present to enable idempotent hook behavior.

## CLI reference

```bash
car apps list [--repo .] [--json]
car apps show <app-ref-or-id> [--repo .] [--json]
car apps install <app-ref> [--repo .] [--force] [--json]
car apps installed [--repo .] [--json]
car apps tools <app-id> [--repo .] [--json]
car apps apply <app-ref-or-id> [--repo .] [--at N] [--next] [--template NAME] [--set key=value]... [--json]
car apps run <app-id> <tool-id> [--repo .] [--timeout SECONDS] [--json] -- <tool-args...>
car apps artifacts <app-id> [--repo .] [--run-id RUN_ID] [--json]
```

App references use the format `REPO_ID:APP_PATH[@REF]`. For example,
`blessed:apps/autooptimize@main` means fetch the app bundle directory
`apps/autooptimize` from app repo `blessed` at the `main` ref.

## Migrating AutoOptimize installs

Older development installs of `blessed.autooptimize` may have provenance from
the CAR core repository path. The app id stays `blessed.autooptimize`, but the
source now becomes `blessed:apps/autooptimize@main` from the external blessed
app repo.

Use a force reinstall to replace the locked bundle while preserving the
app-owned state directory:

```bash
car apps install blessed:apps/autooptimize --repo /path/to/repo --force
```

## Example fixture

See `tests/fixtures/apps/echo-workflow/` for a complete minimal app bundle
that exercises manifest parsing, tool execution, state writing, artifact
production, and the `after_ticket_done` hook.

## Schema drift

This repo does not currently have automated schema drift checks for the
app manifest format. If the repo adopts a drift check mechanism for comparable
contracts (see `docs/reference/hub-manifest-schema.md` for an example), the
`car-app.yaml` manifest schema should be added to that mechanism.
