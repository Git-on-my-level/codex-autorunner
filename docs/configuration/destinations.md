# Destinations

Destinations control where agent backends execute for each repo/worktree in hub mode.

## Summary

- Default destination is `local`.
- Destination config is stored per repo entry in `<hub_root>/.codex-autorunner/manifest.yml`.
- Worktrees inherit destination from their base repo unless the worktree sets an explicit override.

## Destination Kinds

### Local (default)

Local execution uses the host environment exactly as before.

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
  env_passthrough:
    - CAR_*
    - OPENAI_API_KEY
  mounts:
    - source: /opt/shared-cache
      target: /opt/shared-cache
```

Notes:
- `image` is required for `kind: docker`.
- The repo path is always bind-mounted automatically as `${REPO_ROOT}:${REPO_ROOT}`.
- Extra `mounts` are optional.
- `env_passthrough` supports wildcard patterns (for example `CAR_*`).
- `container_name` is optional; CAR chooses a deterministic default when omitted.
- Docker destination also overrides app-server supervisor state root to:
  - `<repo_root>/.codex-autorunner/app_server_workspaces`
- Why: docker execution is bound to the repo mount, so supervisor/workspace state must live in a path that is available and writable from that mount.
- This remains canonical because it is still under `.codex-autorunner/` (repo-local root), not a shadow location.

## CLI Usage

Show effective destination for a repo/worktree:

```bash
car hub destination show <repo_id> --path <hub_root>
```

Set destination to local:

```bash
car hub destination set <repo_id> local --path <hub_root>
```

Set destination to docker:

```bash
car hub destination set <repo_id> docker \
  --image ghcr.io/your-org/your-image:latest \
  --name car-ws-demo \
  --env CAR_* \
  --env OPENAI_API_KEY \
  --mount /opt/shared-cache:/opt/shared-cache \
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

Use `car hub destination show <worktree_repo_id>` to verify the effective source (`repo`, `base`, or `default`).

## Troubleshooting

Destination checks are included in doctor output:

```bash
car doctor --repo <hub_root>
```

Common issues:
- `docker destination requires non-empty 'image'`: add `image`.
- `unsupported destination kind`: use `local` or `docker`.
- invalid mounts shape: provide `source` and `target` strings.

If docker-backed backends fail to start:
- Verify docker is installed and running (`docker --version`, `docker ps`).
- Verify the configured image is pullable.
- Verify required env vars are present in the host environment for passthrough.
- Verify any configured extra mounts exist on the host.

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
