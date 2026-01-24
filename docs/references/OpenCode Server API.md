# OpenCode Server API — Developer Reference

## 1) Overview

**What it is**
The OpenCode Server is a **headless HTTP server** exposing an **OpenAPI 3.1 API** that lets you interact with OpenCode programmatically (beyond the TUI/CLI). Internally the TUI is just a client to this API. citeturn0view0

**Primary use cases**
- Drive sessions, messages, commands remotely
- Inspect and modify project state
- Integrate OpenCode into tools (IDE plugins, web UIs, automation)
- Generate an SDK client from the OpenAPI spec citeturn0view0

---

## 2) Running the Server

**Start server**

```bash
opencode serve [--port <number>] [--hostname <string>] [--cors <origin>]
```

**Defaults**
- `port`: `4096`
- `hostname`: `127.0.0.1`
- `cors`: none (passable multiple times) citeturn0view0

**Common flags**
- `--mdns`: enable mDNS discovery
- `--cors`: add allowed origins (for browser clients) citeturn0view0

**Authentication**
- Protect with HTTP Basic Auth via `OPENCODE_SERVER_PASSWORD`.
- Username defaults to `opencode` (override with `OPENCODE_SERVER_USERNAME`). citeturn0view0

---

## 3) API Spec

The server exposes a **OpenAPI 3.1 spec** at:

```
http://<hostname>:<port>/doc
```

This spec is authoritative for code generation, model types, request/response shapes, and SDK generation. citeturn0view0

Use it with Swagger/OpenAPI tooling to generate TypeScript/Python clients or validate requests.

---

## 4) Core API Groups

Below are the primary collections of endpoints you’ll integrate with:

### A. **Global**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/global/health` | Server health + version |
| GET | `/global/event` | SSE event stream |

Used to check availability and stream global events. citeturn0view0

---

### B. **Project**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/project` | List all projects |
| GET | `/project/current` | Get current project |

Useful to discover context/projects the server is aware of. citeturn0view0

---

### C. **Sessions**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/session` | List sessions |
| POST | `/session` | Create new session |
| GET | `/session/:id` | Get session details |
| DELETE | `/session/:id` | Delete session |
| PATCH | `/session/:id` | Update session metadata |

Sessions represent interactive contexts (like conversations). You’ll start and manage them here. citeturn0view0

**Actions**
- Fork, abort, share, summarize, revert — all supported via session subpaths. citeturn0view0

---

### D. **Messages**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/session/:id/message` | List messages |
| POST | `/session/:id/message` | Send a message and wait |
| POST | `/session/:id/prompt_async` | Async message (no wait) |

This is central to sending prompts, content, or commands into a session (i.e., agent/model). citeturn0view0

---

### E. **Commands**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/command` | List all available slash commands |

Useful for tooling that wants to expose built-in commands (e.g., `/open`, `/fix`). citeturn0view0

---

### F. **Files & Search**
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/find?pattern=...` | Fuzzy search files |
| GET | `/file?path=...` | List files/folders |
| GET | `/file/content?path=...` | Read file content |

Programmatic project exploration (search/code browsing). citeturn0view0

---

## 5) Extended & Experimental

### Tools (experimental)
- Endpoints to list tools by provider/x model with JSON schemas. citeturn0view0

### LSP / Formatters / MCP
- Query status of LSP servers, formatters, MCP context servers. citeturn0view0

### TUI Control
- Endpoints specifically to drive the TUI programmatically (append prompt, open help, etc.). citeturn0view0

---

## 6) Typical Integration Patterns

### Create Session + Message Loop

1. `POST /session` → new session
2. `POST /session/:id/message` → send prompt to model
3. `GET /session/:id/message` → streaming/batch results

Use REST or SSE to handle streaming if needed.

---

### File/Project Inspection

1. `GET /file?path=` → list workspace files
2. `GET /find?pattern=` → search code
3. `GET /file/content?path=` → read content

Useful for editor integrations or programmatic code queries.

---

## 7) Auth & CORS Notes

- HTTP Basic Auth required for network use; unset in local only.
- Set CORS via CLI (`--cors`) if consuming from web clients. citeturn0view0

---

## 8) SDKs & Tooling

Official SDKs (e.g., JavaScript/TypeScript) are available and auto-generate from the server API spec — use them where possible to avoid manual HTTP boilerplate. citeturn0search7

---

## 9) Deployment & Config

Server behavior (port, hostname, mDNS discovery, CORS) can be configured via:
- CLI flags to `opencode serve`
- `opencode.json` under the `"server"` key citeturn0search12

---

## 10) Checklist for API Consumers

**Before you start**
- Start/Open server with `opencode serve`
- Ensure auth configured (`OPENCODE_SERVER_PASSWORD`)
- Fetch OpenAPI spec at `/doc`
- Generate client (SDK)

**Core workflows**
- Session management
- Message input/streaming
- File/project inspection
- Command discovery
- Event streaming
