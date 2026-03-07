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

For launchd/systemd local hub installs, the safe refresh/install scripts provision
the `browser` extra and install Playwright Chromium automatically.

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
- `--project-root PATH`
- `--project-context/--no-project-context` (defaults to enabled)

By default, CAR tears down the spawned serve process tree on success, timeout, failure, and interruption.
`car render demo --keep-session` is the explicit exception: it keeps the serve process alive and writes
session metadata under `.codex-autorunner/render_sessions/`.

When project context is enabled, CAR resolves project root from `--project-root` or repo root, adds
project bins to `PATH` (`node_modules/.bin`, `.venv/bin` or `.venv/Scripts`, `.codex-autorunner/bin`, `bin`),
and uses project root as serve command cwd when `--cwd` is not set. Use `--no-project-context` to preserve
legacy inherited cwd/PATH behavior.

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

## Demo Preflight

Use demo preflight to validate a manifest against a live target before capture:

- `--preflight`: run preflight, then capture only if preflight passes.
- `--preflight-only`: run preflight and skip `capture_demo`.
- `--preflight-report PATH`: write a JSON diagnostics report with per-step status/details.

Example (gate capture on preflight):

```bash
car render demo \
  --url http://127.0.0.1:3000 \
  --script tests/fixtures/browser_demo_manifest.yaml \
  --preflight \
  --preflight-report .codex-autorunner/filebox/outbox/demo-preflight.json
```

Example (preflight-only smoke check):

```bash
car render demo \
  --url http://127.0.0.1:3000 \
  --script tests/fixtures/browser_demo_manifest.yaml \
  --preflight-only
```

Preflight output includes actionable per-step diagnostics in stdout and report JSON, including failed step index/action/detail.

## Attachable Demo Sessions

You can keep a serve-mode demo session alive and later attach to it:

- `--session-id TEXT`: required identifier for keep/attach.
- `--keep-session/--no-keep-session`: serve mode only, default `--no-keep-session`.
- `--attach-session`: URL mode only, reuses an existing kept session instead of starting `--serve-cmd`.

Start + keep a serve session:

```bash
car render demo \
  --serve-cmd "python tests/fixtures/browser_fixture_app.py --port 4173 --pid-file /tmp/browser-fixture.pid" \
  --ready-url http://127.0.0.1:4173/health \
  --script tests/fixtures/browser_demo_manifest.yaml \
  --session-id fixture-4173 \
  --keep-session
```

Attach to the kept session later:

```bash
car render demo \
  --url http://127.0.0.1:4173 \
  --script tests/fixtures/browser_demo_manifest.yaml \
  --session-id fixture-4173 \
  --attach-session
```

If session metadata is missing or stale, CAR exits with explicit `session_missing` / `session_stale` diagnostics.

## Self-Describe Signals

`car describe --json` includes render/browser feature flags under `features`:
- `render_cli_available`
- `browser_automation_available`

Use this for agent-side capability checks before attempting render workflows.
