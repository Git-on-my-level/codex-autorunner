# OpenCode in CAR

CAR uses OpenCode through `opencode serve` and the HTTP/SSE API in
`src/codex_autorunner/agents/opencode/`.

## Runtime Shape

- `OpenCodeSupervisor` owns the server process and client handles.
- `OpenCodeClient` calls `/session`, `/session/{id}/prompt_async`, `/session/{id}/abort`,
  `/session/status`, and the event stream.
- `OpenCodeHarness` adapts OpenCode sessions to CAR managed-thread turns.
- `run_opencode_prompt` is used by review flows that run a temporary OpenCode
  session and dispose it afterwards.

## Agent Files

CAR writes generated OpenCode agents under `.opencode/agent/<agent-id>.md`.
OpenCode 1.15.13 also reads `.opencode/agents/`, but CAR writes the singular
path.

Generated files are marked with:

```yaml
car_managed: codex-autorunner
```

CAR may update marked files and old minimal CAR-generated files. User-authored
agent files with custom frontmatter/body are left unchanged.

## Review Task Policy

OpenCode Task subagents are a reliability fault boundary. CAR review now uses:

- a primary coordinator agent with `permission.task` denying `*` and allowing
  only the configured read-only review subagent
- a read-only subagent with write-like permissions denied: `edit`, `write`,
  `bash`, and `todowrite`

The parent coordinator still owns scratchpad/final report writing. Subagents
should return analysis to the coordinator; they should not edit files or write
artifacts directly.

Use `scripts/probe_opencode_agent_policy.py` to verify the installed OpenCode
binary parses these permissions as expected.

## Timeouts and Stalls

CAR has its own OpenCode stream stall handling:

- `opencode.session_stall_timeout_seconds` configures CAR-side stream stall
  behavior.
- Busy `session.status` heartbeats do not count as meaningful progress.
- The stream lifecycle reconnects, polls session status, and can abort the
  primary session on timeout.

OpenCode provider options such as provider `timeout` and `chunkTimeout` are
OpenCode configuration, not CAR HTTP client timeouts. Verify exact OpenCode
provider config shape before generating or documenting repo defaults for them.

## Known Reliability Boundaries

- CAR can abort the primary OpenCode session.
- CAR observes descendant sessions for progress, but it does not currently have
  a proven API contract for killing one Task child while allowing the parent to
  continue.
- Rescue should be implemented only after descendant-session telemetry and abort
  cascade behavior are verified.
