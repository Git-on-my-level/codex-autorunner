# Web UI Security Posture

This document summarizes the security surface of the web UI and API. It is
intended for operators who want to understand the risks of exposing the web
interface and how to secure it.

## Scope and threat model

- The web server exposes a FastAPI HTTP API and the Web Hub web UI.
- The UI/API can run code and modify files in bound workspaces.
- There is no built-in multi-user auth or per-endpoint role separation.

## Internet-accessible surfaces

Treat any internet-accessible CAR web deployment as a privileged remote-control
surface for the host account running CAR:

- Authenticated users can access terminal WebSockets, run controls, system
  update routes, filebox uploads, contextspace and ticket mutation routes, and
  repo/worktree management routes, and Preview Services proxy routes.
- CAR does not provide read-only users, least-privilege roles, or per-repo
  browser authorization. A valid browser session or bearer token is equivalent
  to administrative access to the exposed hub.
- The only intentionally unauthenticated web routes are the public endpoints
  listed below. They are kept narrow for health checks, static asset loading, and
  first browser bootstrap.
- Public internet deployments should use HTTPS, explicit `server.allowed_hosts`,
  explicit `server.allowed_origins`, bootstrap browser auth, and preferably an
  additional reverse-proxy auth layer such as SSO or basic auth.
- Do not expose a repo app or hub directly over plain HTTP on a public network.
  Remote bootstrap is designed to fail closed unless the app receives a trusted
  HTTPS request scheme.

## Bootstrap browser auth

Remote hub deployments can use a one-time bootstrap token to create a browser
session without putting a bearer token in browser storage:

- On remote boot, CAR writes a high-entropy token to
  `.codex-autorunner/bootstrap-token` with `0600` permissions.
- Open `https://host/auth/bootstrap#token=YOUR_BOOTSTRAP_TOKEN`.
- The browser posts the fragment token to `/auth/bootstrap/claim`; URL fragments
  are not sent in HTTP requests.
- The bootstrap claim endpoint accepts the browser `Origin` from an HTTPS
  reverse proxy only when that origin host matches the request `Host`, so first
  login does not require pre-populating `server.allowed_origins`; Host header
  validation still applies.
- A successful claim deletes the bootstrap token and sets an HttpOnly Secure
  `car_session` cookie with `SameSite=Lax`.
- Bootstrap tokens expire after 24 hours. Restarting the hub after expiration
  writes a fresh token.
- Because the session cookie is `Secure`, expose remote browser deployments over
  HTTPS, including when a reverse proxy terminates TLS in front of CAR. Remote
  bootstrap claims over plain HTTP are rejected; reverse proxies must pass a
  trusted HTTPS request scheme to the ASGI app.

To recover or rotate browser access, use `/auth/session/revoke` for the current
browser session or `/auth/sessions/revoke-all` for all browser sessions. If the
hub is inaccessible, remove `.codex-autorunner/browser-sessions.json`, remove
any stale `.codex-autorunner/bootstrap-token`, and restart the hub. CAR will
write a fresh bootstrap token on the next authenticated or remote boot.

## API bearer token

CAR also supports a bearer token enforced by middleware when configured:

- Set `server.auth_token_env` in `.codex-autorunner/config.yml`.
- Export the token in the environment before starting the server.
- API and CLI requests can use `Authorization: Bearer <token>`.
- WebSockets accept the token via `Sec-WebSocket-Protocol: car-token-b64.<base64url(token)>`.
  The legacy `?token=...` query string is still accepted for backward compatibility,
  but base-path redirects strip CAR-reserved query parameters such as
  `car_token`, `car_auth_token`, and `car_preview_token` to avoid writing CAR
  credentials into redirect logs.

`auth.mode=hosted_bearer` is intended for hosted/no-subdomain deployments where
same-origin preview JavaScript must not be able to call hub APIs through ambient
browser cookies. In this mode, browser session cookies are deliberately not
accepted for hub/control-plane APIs; clients must send an explicit bearer token.
Do not expose the raw `server.auth_token_env` value to browser JavaScript or
store hub access tokens in `localStorage`, `sessionStorage`, cookies, or
IndexedDB. A browser-side hosted bearer bootstrap that mints derived,
short-lived, in-memory hub tokens is not part of this release unless supplied by
an outer auth layer.

## Public endpoints

The following endpoints remain public so health checks and static assets work:

- `/` (UI shell)
- `/auth/bootstrap` and `/auth/bootstrap/claim`
- `/_app/*`
- `/health`
- `/cat/*`

All API endpoints, hub endpoints, repo endpoints, and deep-linked UI routes
require a valid browser session or bearer token once auth is enabled.

## Localhost hardening (Host/Origin checks)

When auth is disabled, CAR still enforces basic browser protections to reduce
localhost CSRF/DNS rebinding risk:

- Host header allowlisting: loopback binds default to `localhost`, `127.0.0.1`,
  and `::1` (port variations allowed).
- Origin validation: for unsafe methods (POST/PUT/PATCH/DELETE) and WebSocket
  handshakes, requests with an `Origin` header must match the server origin or
  an explicit allowlist.
- CLI and non-browser clients continue to work because requests without an
  `Origin` header are allowed.

Config:

- `server.allowed_hosts`: explicit host allowlist. Required when binding to a
  non-loopback host. Do not use `*` for internet-exposed deployments; wildcard
  hosts disable Host-header protection.
- `server.allowed_origins`: extra allowed origins (scheme + host + port).
  Configure this for normal browser API/WebSocket use when CAR is exposed
  through a public reverse proxy.

## Preview Services

Local trusted preview routes under `/preview/services/<service_id>/` require the
same hub authentication as the rest of the protected web surface. In hosted
bearer mode, hub/control-plane APIs require `Authorization: Bearer <token>` and
copied preview links use `/preview/p/<capability>/...` URLs. Preview capability
tokens authorize only preview/proxy access and cannot authorize `/hub/*` APIs.

CAR proxies only registered service records, defaults proxy targets to loopback
hosts, and does not proxy arbitrary internet URLs.

Preview capability URLs are bearer credentials for their specific preview
service. Treat `/preview/p/<token>/...` paths as secrets: do not log raw
capability paths, do not include them in structured events except when returning
an issued link to the authenticated caller, set preview responses to avoid
referrer leakage, strip upstream `Referer` forwarding from proxied preview
requests, strip `Service-Worker-Allowed` from preview responses, and prefer
private/no-store cache semantics for hosted capability responses.

Keep `preview_services.proxy_allowed_hosts` loopback-only for hosted
deployments. Add `preview_services.static_allowed_roots` only for directories
you intentionally want CAR to serve as previews; static previews reject
traversal, symlink escape, hidden files, `.env`, `.git`, and common private-key
filenames. See [Preview Services](../ops/preview-services.md) for operator
recipes and HMR caveats.

## Recommendations

- Prefer local-only access (`127.0.0.1`) or a private network like Tailscale.
- If exposing the server beyond localhost, use bootstrap browser auth and keep
  `server.auth_token_env` available for automation.
- Use a reverse proxy with additional auth (basic auth, SSO) if you must put it
  on the public internet.
- Avoid placing the web UI behind a publicly accessible hostname without
  explicit authentication.
- Treat the web UI as privileged access, equivalent to shell access on the host.

## References

- `README.md` (Security and remote access)
