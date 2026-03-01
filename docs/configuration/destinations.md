# Destinations

Destinations control where agent backends execute for each repo/worktree in hub mode.

## Summary

- Default destination is `local`.
- Destination config is stored per repo entry in `<hub_root>/.codex-autorunner/manifest.yml`.
- Worktrees inherit destination from their base repo unless the worktree sets an explicit override.
- Canonical schema reference: [Hub Manifest Destination Schema](../reference/hub-manifest-schema.md).

## Destination Kinds

### Local (default)

Local execution uses the host environment.

```yaml
destination:
  kind: local
```

### Docker

Docker execution wraps backend commands with `docker exec` against a managed container.

```yaml
destination:
  kind: docker
  image: ghcr.io/your-org/your-image:latest
  container_name: car-ws-demo
  profile: full-dev
  workdir: /workspace/repo
  env_passthrough:
    - CAR_*
    - OPENAI_API_KEY
  env:
    CODEX_HOME: /workspace/repo/.codex
  mounts:
    - source: /opt/shared-cache
      target: /opt/shared-cache
    - source: /opt/readonly-cache
      target: /opt/readonly-cache
      read_only: true
```

Supported Docker fields in manifest/API payloads:
- `kind` (`docker`) and `image` (required for docker).
- `container_name` (optional).
- `profile` (optional, currently only `full-dev` is supported).
- `workdir` (optional; defaults to repo root path in container).
- `env_passthrough` (optional list of passthrough patterns like `CAR_*`).
- `env` (optional explicit env map of `KEY: VALUE` string pairs).
- `mounts` (optional list with `source`, `target`, optional `read_only` boolean).

Notes:
- The repo path is always bind-mounted automatically as `${REPO_ROOT}:${REPO_ROOT}`.
- The canonical mount flag is `read_only` in manifest storage.
- API requests also accept `readOnly` and `readonly` aliases, normalized to `read_only`.
- Docker destination overrides app-server supervisor state root to:
  - `<repo_root>/.codex-autorunner/app_server_workspaces`
- This keeps state under repo-local `.codex-autorunner/` and writable from the repo mount.

## `full-dev` Profile Contract

`full-dev` is the only supported docker profile. It contributes defaults and enforces preflight checks:

- Default passthrough env patterns:
  - `CAR_*`
  - `OPENAI_API_KEY`
  - `CODEX_HOME`
  - `OPENCODE_SERVER_USERNAME`
  - `OPENCODE_SERVER_PASSWORD`
- Default mounts:
  - `${HOME}/.codex` -> `${HOME}/.codex`
  - `${HOME}/.local/share/opencode` -> `${HOME}/.local/share/opencode`
- Required binaries (container preflight):
  - `codex`, `opencode`, `python3`, `git`, `rg`, `bash`, `node`, `pnpm`
- Required readable auth files (container preflight):
  - `${HOME}/.codex/auth.json`
  - `${HOME}/.local/share/opencode/auth.json`

## Mount, Env, and Credential Expectations

- Host paths in `mounts[].source` must exist and be accessible to the docker daemon context.
- `mounts[].target` must be valid inside the container image.
- `env_passthrough` only forwards variables that exist in the host environment and match configured patterns.
- `env` map values are explicit and override same-name passthrough vars.
- For `profile: full-dev`, ensure both auth files exist and are readable in the container path mapping before running flows.

## CLI Usage

Show effective destination for a repo/worktree:

```bash
car hub destination show <repo_id> --path <hub_root>
```

Set destination to local:

```bash
car hub destination set <repo_id> local --path <hub_root>
```

Set destination to docker (including profile/workdir/env-map/read-only mount):

```bash
car hub destination set <repo_id> docker \
  --image ghcr.io/your-org/your-image:latest \
  --name car-ws-demo \
  --profile full-dev \
  --workdir /workspace/repo \
  --env CAR_* \
  --env OPENAI_API_KEY \
  --env-map CODEX_HOME=/workspace/repo/.codex \
  --mount /opt/shared-cache:/opt/shared-cache \
  --mount-ro /opt/readonly-cache:/opt/readonly-cache \
  --path <hub_root>
```

JSON output for automation:

```bash
car hub destination show <repo_id> --json --path <hub_root>
car hub destination set <repo_id> docker --image busybox:latest --json --path <hub_root>
```

## Inheritance Rules

Given a worktree repo entry:
- Use the worktree destination if set and valid.
- Else inherit destination from base repo if set and valid.
- Else use `{"kind":"local"}`.

Use `car hub destination show <worktree_repo_id>` to verify effective source (`repo`, `base`, or `default`).

## Troubleshooting

Destination checks are included in doctor output:

```bash
car doctor --repo <hub_root>
```

Common issues:
- `docker destination requires non-empty 'image'`: add `image`.
- `unsupported destination kind`: use `local` or `docker`.
- `mounts[0].read_only must be a boolean`: pass `true`/`false`, not strings.
- `env['KEY'] must be a string value`: env map values must be strings.

If docker-backed backends fail to start:
- Verify docker is installed and running (`docker --version`, `docker ps`).
- Verify the configured image is pullable.
- Verify required env vars are present in the host environment for passthrough.
- Verify configured extra mounts exist on the host.
- For `full-dev`, verify required auth files are present and readable.

## Smoke Procedure

Use the safe-by-default script:

```bash
scripts/smoke_destination_docker.sh --hub-root <hub_root> --repo-id <repo_id>
```

Dry-run is default. To execute, pass:

```bash
scripts/smoke_destination_docker.sh \
  --hub-root <hub_root> \
  --repo-id <repo_id> \
  --image busybox:latest \
  --execute
```

The script captures destination/status/log evidence under:
- `<hub_root>/.codex-autorunner/runs/destination-smoke-<timestamp>/`
