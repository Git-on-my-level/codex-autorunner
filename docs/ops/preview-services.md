# Preview Services

Preview Services expose local preview resources through CAR URLs. Local/trusted
authenticated service routes use:

```text
/preview/services/<service_id>/
```

Use CAR preview URLs for user-facing links. Direct `localhost` URLs are kept only
as diagnostics because they do not work reliably from mobile chat clients,
remote browser sessions, or hosted CAR deployments.

Hosted/no-subdomain deployments copy and open preview capability URLs such as
`/preview/p/<token>/`; those tokens authorize preview access only and do not
grant hub API access.

## Configuration

Preview Services are configured on the hub:

```yaml
preview_services:
  enabled: true
  port_range:
    start: 39000
    end: 39999
  default_host: "127.0.0.1"
  proxy_allowed_hosts:
    - "127.0.0.1"
    - "::1"
    - "localhost"
  static_allowed_roots: []
  log_max_bytes: 1048576
  log_tail_default_lines: 200
  proxy_max_body_bytes: 10485760
  proxy_connect_timeout_seconds: 5.0
  proxy_read_timeout_seconds: 60.0
  proxy_write_timeout_seconds: 60.0
  proxy_pool_timeout_seconds: 5.0
  proxy_max_global_streams: 128
  proxy_max_service_streams: 16
  auto_start_on_hub_start_default: false
```

Defaults are safe for local use and require no setup. Keep
`proxy_allowed_hosts` loopback-only unless CAR adds an explicit non-loopback
policy for your deployment. Add `static_allowed_roots` only for directories you
intend CAR to serve as previews; filesystem roots and parent traversal are
rejected.

## CLI Recipes

Register a static HTML file:

```bash
car services register-static ./dist/index.html --name "Static preview" --kind static-file
car services list
```

Register a static directory:

```bash
car services register-static ./dist --name "Built site" --kind static-dir
```

Register an existing local API or web server:

```bash
car services register-url http://127.0.0.1:8000 --name "Local API" --health-path /health
```

Start a managed Vite dev server with CAR port allocation:

```bash
car services start-managed --name "Vite app" --cwd . --auto-port -- npm run dev -- --host 127.0.0.1 --port '$PORT'
```

Start a managed Next dev server:

```bash
car services start-managed --name "Next app" --cwd . --auto-port -- npm run dev -- --hostname 127.0.0.1 --port '$PORT'
```

Inspect, restart, and read logs:

```bash
car services list --json
car services get SERVICE_ID
car services logs SERVICE_ID --tail 200
car services restart SERVICE_ID
car services health SERVICE_ID
```

Stop or remove a service:

```bash
car services stop SERVICE_ID
car services teardown SERVICE_ID
```

Forceful actions require an attestation:

```bash
car services kill SERVICE_ID --force --force-attestation "Terminate the stuck preview process"
car services unlink SERVICE_ID --force --force-attestation "Remove stale registry entry"
```

Autostart is opt-in:

```bash
car services set-autostart SERVICE_ID --enabled
car services set-autostart SERVICE_ID --disabled
```

Services do not restart after hub restart unless autostart is explicitly enabled
for that service.

## Services Page

The Web UI Services page lists registered previews, status, health, scope,
ports, owner, uptime, and CAR URLs. Use it to open or copy preview links, view
managed-service logs, and run lifecycle actions.

Available actions depend on service kind. Static and loopback services can be
opened, copied, unlinked, or torn down. Managed command services can also be
started, stopped, restarted, killed with confirmation, and tailed for logs.
The backend computes the action capabilities in the read model; UI, CLI, and
PMA should not infer permissions from `kind` or `status` alone.

Preview links opened from the Services page must use a separate browsing
context with opener isolation (`noopener,noreferrer`). Do not embed untrusted
same-origin previews in an unsandboxed iframe.

## Service Taxonomy

`kind` describes the implementation substrate:

- `static_file` and `static_dir` serve registered files or directories.
- `loopback_url` proxies an already-running local service.
- `managed_command` starts and supervises a CAR-owned subprocess.

Every service also carries product and trust metadata:

- `service_class=preview` for generated artifacts and agent-created dev servers.
- `service_class=application` for user-registered local apps and APIs.
- `service_class=infrastructure` for trusted supporting systems such as
  OpenCode servers, MCP servers, webhook receivers, tunnels, browser automation
  services, vector stores, and future Codex bridges.
- `trust_level` is `generated`, `external`, or `trusted`.
- `ownership` is `static`, `external`, or `car_managed`.
- `network_policy` defaults to `loopback_only`.

OpenCode and MCP examples should be modeled as infrastructure services in the
registry, not as side channels outside Preview Services. Codex app-server is not
CAR's durable service manager; CAR's registry and supervisor remain the source
of truth for managed infrastructure.

## HMR Caveats

Hot reload works best when the dev server is bound to `127.0.0.1`, uses the
CAR-allocated `$PORT`, and is configured for same-origin HMR. Vite, SvelteKit,
and Next-style flows generally work when the browser connects back to the same
CAR preview URL.

CAR serves previews below a path prefix:

```text
/preview/services/<service_id>/
```

Many frameworks assume they are mounted at `/` unless configured otherwise. For
reliable assets, redirects, client routing, and HMR, configure the app's base
path to match the CAR preview prefix. Managed commands receive these environment
variables:

```text
CAR_PREVIEW_BASE_PATH=/preview/services/<service_id>
CAR_PREVIEW_PUBLIC_URL=/preview/services/<service_id>/
CAR_PREVIEW_SERVICE_ID=<service_id>
PORT=<allocated_port>
HOST=127.0.0.1
```

Framework notes:

- Vite: set `base` to `process.env.CAR_PREVIEW_BASE_PATH + "/"`. Prefer
  same-origin HMR; if you customize HMR, keep the client path and host aligned
  with the CAR preview URL instead of hard-coding `localhost`.
- SvelteKit: set `kit.paths.base` from `CAR_PREVIEW_BASE_PATH`. Root-relative
  assets or links outside that base will bypass the preview prefix.
- Next.js: `basePath` can mount pages below the preview prefix, but dev-mode
  internals and `assetPrefix` have caveats. Verify script and HMR URLs in the
  browser when using `next dev`.
- React Router: create the router with `basename` set to
  `CAR_PREVIEW_BASE_PATH` so client-side navigation stays under the preview URL.

CAR does not rewrite arbitrary application bundles that hard-code absolute
`http://localhost:*` or `ws://localhost:*` URLs. If HMR fails, configure the dev
server websocket host/client URL to use same-origin behavior, then restart the
service.

## Hosted / No-Subdomain Security Model

Local trusted deployments may keep `/preview/services/<service_id>/` protected
by the same browser session or bearer token as the hub.

Hosted or otherwise untrusted deployments that cannot rely on dynamic
subdomains must not authorize hub APIs with ambient browser credentials from
same-origin preview JavaScript. In `auth.mode=hosted_bearer`:

- `/hub/*`, read models, lifecycle routes, and other control-plane APIs require
  `Authorization: Bearer <hub_access_token>`.
- Cookies, Basic Auth, and preview capability tokens do not authorize hub APIs.
- User-facing copied/opened preview links use capability URLs such as
  `/preview/p/<token>/...`.
- A preview capability token authorizes only preview/proxy access for its
  service and cannot be used as a hub bearer token.
- Capability tokens expire and can be revoked per service with
  `car services revoke-link SERVICE_ID --all`.
- Authenticated requests to `/preview/services/<service_id>/...` may redirect to
  a capability URL for local/trusted compatibility, but remote links should use
  the capability URL.

A hosted CAR deployment should use HTTPS, explicit `server.allowed_hosts`,
explicit origins, bearer-token hub API auth, and an outer reverse-proxy auth
layer when exposed beyond a trusted private network.

CAR proxies only registered service IDs. By default, proxy targets are limited
to loopback hosts (`127.0.0.1`, `::1`, and `localhost`) and CAR does not proxy
arbitrary internet URLs. Static previews are restricted to allowed roots and
reject traversal, symlink escape, hidden files, `.env`, `.git`, and common
private-key filenames.

## Managed Command Operations

Managed services run as subprocesses in process groups, not tmux sessions. CAR
records pid/pgid, command metadata, bounded logs, health state, event history,
and observed exits under `.codex-autorunner/services/`.

Lifecycle mutations use per-service locks. Stop is graceful with a timeout; kill
is destructive and requires `--force --force-attestation TEXT`. Restart is stop
then start from the stored service definition. Teardown stops a managed service
and then removes the registry record.

Before stopping or killing a process after a restart or crash, CAR checks process
identity. If the recorded pid no longer matches the expected process, normal
termination is refused and the service is marked `orphaned` instead of risking a
stale-PID kill.

Autostart after hub restart is disabled by default and must be enabled per
service. Exited, failed, unhealthy, conflict, and orphaned services are marked as
needing attention for the UI, CLI, and PMA snapshot.

## Environment Policy

Managed commands default to `env_policy=minimal`. They receive explicit
per-service env overrides plus CAR preview variables:

```text
CAR_PREVIEW_BASE_PATH=/preview/services/<service_id>
CAR_PREVIEW_PUBLIC_URL=/preview/services/<service_id>/
CAR_PREVIEW_SERVICE_ID=<service_id>
PORT=<allocated_port>
HOST=127.0.0.1
```

The full CAR process environment is inherited only when `env_policy=inherit_all`
is explicitly requested. Read models redact configured env values, and service
logs should not be copied wholesale into PMA prompts.

## PMA and CLI Context

PMA receives a compact Preview Services snapshot with counts, attention items,
and a small sample of running links. For details or lifecycle actions, PMA and
agents should use the CLI:

```bash
car services list --json
car services get SERVICE_ID --json
car services logs SERVICE_ID --tail 200
car services health SERVICE_ID
car services issue-link SERVICE_ID --ttl 24h
car services revoke-link SERVICE_ID --all
```

CLI path arguments are resolved on the client side before sending them to the
hub. Use absolute or shell-relative paths normally; avoid sending ambiguous
relative filesystem paths directly to hub APIs.

## Regression Matrix

The high-risk contract is covered by focused tests:

- Hosted previews cannot call hub APIs without a bearer token:
  `tests/surfaces/web/test_preview_services_routes.py`.
- Preview capability issue, expiry, and revocation:
  `tests/surfaces/web/test_preview_services_routes.py` and
  `tests/test_cli_services.py`.
- Opener isolation and no persistent browser token storage:
  `src/codex_autorunner/web_frontend/src/routes/services/page.test.ts`.
- Process exit reconciliation, stale PID safety, lifecycle lock concurrency,
  event history, and slow startup health timing:
  `tests/core/test_preview_services_supervisor.py`.
- Static symlink, hidden path, sensitive path, workspace-root, and ambiguous
  relative path blocking: `tests/surfaces/web/test_preview_services_routes.py`.
- CLI relative path resolution and destructive force payloads:
  `tests/test_cli_services.py`.
- Environment inheritance and read-model redaction:
  `tests/core/test_preview_services_supervisor.py` and
  `tests/core/test_preview_services_registry.py`.
- Proxy compression, redirects, forwarded headers, body limits, streaming, and
  WebSocket/HMR compatibility:
  `tests/surfaces/web/test_preview_services_routes.py`.
