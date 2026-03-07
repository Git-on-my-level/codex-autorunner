# Browser Render Workflows

CAR provides a deterministic, accessibility-first browser capture surface through:
- `car render screenshot`
- `car render observe`
- `car render demo`

This first browser substrate is intentionally not prompt-driven and does not require
Node MCP servers, Stagehand, or other external browser orchestration tooling.

## Install Browser Extra

1. Install CAR with the browser extra:

```bash
pip install "codex-autorunner[browser]"
```

2. Install Playwright browser binaries (Chromium is enough for CAR render flows):

```bash
python -m playwright install chromium
```

If the extra is missing, render commands fail with a clear install hint instead of a stack trace.

## Mode Selection

Use `screenshot` when you need a single page capture (PNG/PDF).

Use `observe` when you need agent-readable page state artifacts (a11y snapshot, metadata, locator references, run manifest).

Use `demo` when you need deterministic, step-by-step interactions from a manifest plus evidence artifacts (screenshots/video by default, optional structured artifacts).

## URL Mode vs Serve Mode

All render commands support URL mode (`--url`) and serve mode (`--serve-cmd`).

Serve mode supports:
- `--ready-url` (preferred)
- `--ready-log-pattern` (fallback)
- `--cwd`
- repeatable `--env KEY=VALUE`

CAR always tears down the spawned serve process tree on success, timeout, failure, and interruption.

## Outbox Artifact Behavior

By default, render outputs go to:

```text
.codex-autorunner/filebox/outbox/
```

Use `--out-dir` or `--output` to override paths.

Artifact naming is deterministic and collision-safe (for example, `-2`, `-3` suffixes on collisions).

## Screenshot Example

```bash
car render screenshot \
  --url http://127.0.0.1:3000 \
  --path /dashboard \
  --format png
```

Serve-mode example:

```bash
car render screenshot \
  --serve-cmd "python tests/fixtures/browser_fixture_app.py --port 4173 --pid-file /tmp/browser-fixture.pid" \
  --ready-url http://127.0.0.1:4173/health \
  --path /
```

## Observe Example

```bash
car render observe \
  --url http://127.0.0.1:3000/settings
```

Typical observe artifacts:
- `observe-a11y-*.json`
- `observe-meta-*.json`
- `observe-locators-*.json`
- `observe-run-manifest-*.json`
- optional `observe-dom-*.html`

## Demo Manifest Example

```yaml
version: 1
steps:
  - action: goto
    url: /form
  - action: fill
    label: Email
    value: demo@example.com
  - action: fill
    label: Password
    value: secret
  - action: click
    role: button
    name: Submit
  - action: wait_for_text
    text: Dashboard
  - action: screenshot
```

Supported v1 actions:
- `goto`
- `click`
- `fill`
- `press`
- `wait_for_url`
- `wait_for_text`
- `wait_ms`
- `screenshot`
- `snapshot_a11y`

Locator preference order:
1. `role` (+ optional `name`)
2. `label`
3. `text`
4. `test_id`
5. `selector` fallback

Run demo with optional trace/video:

```bash
car render demo \
  --serve-cmd "python tests/fixtures/browser_fixture_app.py --port 4173 --pid-file /tmp/browser-fixture.pid" \
  --ready-url http://127.0.0.1:4173/health \
  --path /form \
  --script tests/fixtures/browser_demo_manifest.yaml \
  --record-video \
  --trace on
```

`car render demo` defaults to `--media-only`, which keeps outbox output focused on screenshots/video for end users.
Use `--full-artifacts` when you also want structured JSON/HTML/trace outputs.

## Self-Describe Signals

`car describe --json` includes render/browser feature flags under `features`:
- `render_cli_available`
- `browser_automation_available`

Use this for agent-side capability checks before attempting render workflows.
