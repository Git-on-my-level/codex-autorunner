# Runtime Services Ownership Rule

`RuntimeServices` is the lifecycle owner for CAR-managed, long-lived subprocess resources.

Ownership rule:
- Surfaces and adapters must acquire subprocess-owning resources from a shared `RuntimeServices` instance.
- Do not create long-lived subprocess supervisors ad hoc in request handlers.
- Surface routing keys such as Discord channels, Telegram chats, web tabs, PMA threads, Codex threads, OpenCode sessions, and ticket ids must not key process ownership.
- The only approved spawn owners are:
  - `RuntimeServices`
  - a single manager object owned and closed by `RuntimeServices`

Resources covered:
- `OpenCodeSupervisor`
- `WorkspaceAppServerSupervisor` / `CodexAppServerClient`
- ticket-flow `AgentPool` instances used by cached `FlowController` runtimes

Shutdown rule:
- Every surface startup path that creates `RuntimeServices` must call `await runtime_services.close()` in its shutdown path.
- `RuntimeServices.close()` is idempotent and is responsible for closing all owned resources.

Default topology:
- `app_server.server_scope: global`, with one RuntimeServices-owned Codex app-server supervisor per hub/runtime profile by default.
- `opencode.server_scope: global`, with one RuntimeServices-owned OpenCode supervisor per hub/runtime profile by default.
- Workspace scope remains an explicit isolation/debugging mode; even then, supervisors must be registered with and closed by `RuntimeServices`.
- Ticket-flow `AgentPool` runtimes should reuse `RuntimeServices` Codex/OpenCode supervisors when the enclosing surface provides them.
