# CAR App Manifest Reference (`car-app.yaml`)

The `car-app.yaml` file is the required manifest at the root of every CAR app
bundle directory.

## Schema version

Current version: `1`

```yaml
schema_version: 1
```

## Required fields

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | integer | Must be `1` |
| `id` | string | Stable app id matching `^[a-z0-9][a-z0-9._-]{1,127}$` |
| `name` | string | Human-readable name, must not be empty |
| `version` | string | Semantic-ish version, must not be empty |

## Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | Short description of the app |
| `entrypoint` | mapping | Entrypoint configuration |
| `entrypoint.template` | string | Path to entrypoint ticket template |
| `inputs` | mapping of `{name: AppInput}` | Declared app inputs |
| `templates` | mapping of `{name: AppTemplate}` | Named ticket templates |
| `tools` | mapping of `{name: AppTool}` | Declared executable tools |
| `hooks` | mapping of `{point: [AppHookEntry]}` | Lifecycle hook declarations |
| `permissions` | mapping | Permission restrictions |

## Inputs

```yaml
inputs:
  goal:
    required: true
    description: Optimization goal.
  count:
    required: false
    description: Iteration count.
```

Each input has:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `required` | boolean | `false` | Whether `car apps apply` must receive this input |
| `description` | string | `""` | Human-readable description |

Required inputs that are missing when running `car apps apply` produce an error.
Unknown input keys (not declared in the manifest) are also rejected.

## Templates

```yaml
templates:
  bootstrap:
    path: templates/bootstrap.md
    description: Create the first iteration ticket.
  iteration:
    path: templates/iteration.md
    description: One measurable optimization attempt.
```

Each template has:

| Field | Type | Description |
|-------|------|-------------|
| `path` | string | Bundle-relative path to the template file |
| `description` | string | Human-readable description |

## Tools

```yaml
tools:
  record-iteration:
    description: Append one iteration record to app-owned state.
    argv: ["python3", "scripts/record_iteration.py"]
    timeout_seconds: 30
  render-card:
    description: Render final app artifacts.
    argv: ["python3", "scripts/render_card.py"]
    timeout_seconds: 120
    outputs:
      - kind: image
        path: "artifacts/summary.png"
        label: "Summary card"
      - kind: markdown
        path: "artifacts/summary.md"
        label: "Summary"
```

### Tool fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `description` | string | `""` | Human-readable description |
| `argv` | list of strings | required | Command argument array, must be non-empty |
| `timeout_seconds` | integer | `60` | Maximum execution time |
| `outputs` | list of `AppOutput` | `[]` | Declared output files |

### Output fields

| Field | Type | Description |
|-------|------|-------------|
| `kind` | string | One of: `image`, `markdown`, `text`, `json`, `html` |
| `path` | string | App-runtime-relative path under `.codex-autorunner/apps/<id>/` |
| `label` | string | Human-readable label |

## Hooks

```yaml
hooks:
  after_ticket_done:
    - tool: record-iteration
      when:
        ticket_frontmatter:
          app: blessed.autoresearch
      failure: pause
  after_flow_terminal:
    - tool: render-card
      when:
        status: completed
      failure: warn
  before_chat_wrapup:
    - artifacts:
        - "artifacts/summary.png"
        - "artifacts/summary.md"
```

### Supported hook points

| Hook point | When it fires |
|------------|---------------|
| `after_ticket_done` | After a ticket transitions to `done: true` |
| `after_flow_terminal` | After a ticket-flow run reaches a terminal state |
| `before_chat_wrapup` | Before Discord/Telegram sends the final wrap-up message |

### Hook entry fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tool` | string | optional | Must reference a declared tool id |
| `when` | mapping | `{}` | Selector for when the hook should fire |
| `failure` | string | `"warn"` | One of: `warn`, `pause`, `fail` |
| `artifacts` | list of strings | `[]` | Artifact paths to surface (for `before_chat_wrapup`) |

### When selectors

- `ticket_frontmatter`: mapping of key-value pairs to match against ticket
  frontmatter (including extra fields like `app`).
- `status`: string or list of strings matching the flow run status
  (`completed`, `failed`, `stopped`).

### Failure policies

| Policy | Behavior |
|--------|----------|
| `warn` | Log the failure and continue (default) |
| `pause` | Pause the ticket-flow run |
| `fail` | Fail the ticket-flow run |

## Permissions

```yaml
permissions:
  network: false
  writes:
    - "state/**"
    - "artifacts/**"
  reads:
    - "**"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `network` | boolean | `false` | Whether the app may access the network |
| `writes` | list of glob strings | `[]` | Paths the app may write to |
| `reads` | list of glob strings | `[]` | Paths the app may read from |

## Path rules

All paths in the manifest must follow these rules:

- Must be relative (no leading `/`).
- Must not contain `..`.
- Must not contain backslash separators.
- Must not contain empty segments.
- Globs (`*`, `**`, `?`) are only allowed in permission fields.

### Path scope

- `templates.*.path` and tool `argv` script arguments are **bundle-relative**.
- Tool `outputs.*.path`, hook artifact paths, and permission globs are
  **app-runtime-relative**, rooted at `.codex-autorunner/apps/<app-id>/`.

## Command rules

- Tool commands must use an argv array; shell strings are invalid.
- `argv` must be non-empty.
- `argv[0]` may be a system executable (`python3`, `node`, `bash`), but bundle
  file arguments must resolve under the installed bundle root.
- CAR always executes with `shell=False`.
- CAR sets `cwd` to the target repo root.

## Minimal valid manifest

```yaml
schema_version: 1
id: myorg.example
name: Example App
version: "1.0"
```

## Full manifest example

See `tests/fixtures/apps/echo-workflow/car-app.yaml` for a complete working
example that exercises all major manifest features.
