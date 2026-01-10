# Web UI Security Posture

This document summarizes the security surface of the web UI and API. It is
intended for operators who want to understand the risks of exposing the web
interface and how to secure it.

## Scope and threat model

- The web server exposes a FastAPI HTTP API and a web UI with a terminal-style
  Codex TUI embedded over WebSocket.
- The UI/API can run code and modify files in bound workspaces.
- There is no built-in multi-user auth or per-endpoint role separation.

## Authentication token

CAR supports a bearer token enforced by middleware when configured:

- Set `server.auth_token_env` in `.codex-autorunner/config.yml`.
- Export the token in the environment before starting the server.
- All non-public endpoints require `Authorization: Bearer <token>`.
- WebSockets accept the token via query string `?token=...` because browsers
  cannot always set headers on WS handshakes.

When `server.auth_token_env` is set, the web UI can be accessed by visiting:

```
http://host:port/?token=YOUR_TOKEN
```

The UI stores the token in `sessionStorage` and removes it from the URL.

## Public endpoints

The following endpoints remain public so health checks and static assets work:

- `/` (UI shell)
- `/static/*`
- `/health`
- `/cat/*`

All API endpoints, hub endpoints, and repo endpoints require the auth token
once configured.

## Recommendations

- Prefer local-only access (`127.0.0.1`) or a private network like Tailscale.
- If exposing the server beyond localhost, always set `server.auth_token_env`.
- Use a reverse proxy with additional auth (basic auth, SSO) if you must put it
  on the public internet.
- Avoid placing the web UI behind a publicly accessible hostname without
  explicit authentication.
- Treat the web UI as privileged access, equivalent to shell access on the host.

## References

- `README.md` (Security and remote access)
- `docs/terminal-debugging.md`
