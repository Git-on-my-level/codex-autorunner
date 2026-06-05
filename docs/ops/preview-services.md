# Preview Services

Preview Services expose local preview resources through authenticated CAR URLs:

```text
/preview/services/<service_id>/
```

Use CAR preview URLs for user-facing links. Direct `localhost` URLs are kept only
as diagnostics because they do not work reliably from mobile chat clients,
remote browser sessions, or hosted CAR deployments.

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

## HMR Caveats

Hot reload works best when the dev server is bound to `127.0.0.1`, uses the
CAR-allocated `$PORT`, and is configured for same-origin HMR. Vite, SvelteKit,
and Next-style flows generally work when the browser connects back to the same
CAR preview URL.

CAR does not rewrite arbitrary application bundles that hard-code absolute
`http://localhost:*` or `ws://localhost:*` URLs. If HMR fails, configure the dev
server websocket host/client URL to use same-origin behavior, then restart the
service.

## Hosted Security Model

Preview routes are authenticated like the hub. A hosted CAR deployment should
use HTTPS, browser auth or bearer auth, explicit `server.allowed_hosts`, and an
outer reverse-proxy auth layer when exposed beyond a trusted private network.

CAR proxies only registered service IDs. By default, proxy targets are limited
to loopback hosts (`127.0.0.1`, `::1`, and `localhost`) and CAR does not proxy
arbitrary internet URLs. Static previews are restricted to allowed roots and
reject traversal, symlink escape, hidden files, `.env`, `.git`, and common
private-key filenames.
