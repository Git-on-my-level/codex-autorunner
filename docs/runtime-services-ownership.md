# Runtime Services Ownership Rule

`RuntimeServices` is the lifecycle owner for CAR-managed, long-lived subprocess resources.

Ownership rule:
- Surfaces and adapters must acquire subprocess-owning resources from a shared `RuntimeServices` instance.
- Do not create long-lived subprocess supervisors ad hoc in request handlers.
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
