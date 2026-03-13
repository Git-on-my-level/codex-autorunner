---
title: "Scope the runtime adapter contract and ZeroClaw validation adapter"
agent: "codex"
done: true
goal: "Turn the orchestrator’s adapter expectations into a concrete downstream ticket set, using ZeroClaw as the first generic-agent validation target beyond Codex/OpenCode."
---

## Progress

- [x] Read the adapter-capability and ZeroClaw sections of `.codex-autorunner/contextspace/spec.md`.
- [x] Reviewed the current runtime seams in `src/codex_autorunner/agents/base.py`, `src/codex_autorunner/agents/registry.py`, `src/codex_autorunner/agents/codex/*`, `src/codex_autorunner/agents/opencode/*`, `docs/adding-an-agent.md`, and `docs/plugin-api.md`.
- [x] Reviewed the current public ZeroClaw integration surface closely enough to confirm that durable session/history concepts are visible but a full public session CRUD surface is not clearly documented.
- [x] Wrote `.codex-autorunner/contextspace/runtime-adapter-contract-and-zeroclaw.md` to lock the must-support contract, optional capabilities, current seam gaps, and the honest wrapper path for ZeroClaw.
- [x] Reserved the `420-500` downstream range for runtime adapter, capability, and ZeroClaw validation work from this scope ticket.
- [x] Created downstream tickets:
  - `TICKET-420-expand-runtime-capability-model.md`
  - `TICKET-440-align-agent-harness-with-orchestration-thread-contract.md`
  - `TICKET-460-add-zeroclaw-runtime-adapter-validation-path.md`
  - `TICKET-480-capability-aware-runtime-filtering-for-cli-and-api.md`
  - `TICKET-500-add-adapter-conformance-tests-and-plugin-contract-docs.md`
- [x] Kept durable runtime conversation state downstream and did not use transcript mirroring as a workaround for missing ZeroClaw primitives.

## Tasks
- Read `spec.md` sections on adapter capabilities and ZeroClaw.
- Review the current CAR runtime seams:
  - `src/codex_autorunner/agents/base.py`
  - `src/codex_autorunner/agents/registry.py`
  - `src/codex_autorunner/agents/codex/*`
  - `src/codex_autorunner/agents/opencode/*`
  - `docs/adding-an-agent.md`
  - `docs/plugin-api.md`
- Review the ZeroClaw repo/API enough to confirm the external thread/session surface that CAR must rely on.
- Produce a design memo that states:
  - the exact must-support adapter operations
  - the optional capabilities
  - the precise gaps, if any, between current CAR harnesses and the target contract
  - what the first ZeroClaw adapter/wrapper must provide
- Write downstream implementation tickets for:
  - capability model expansion
  - harness/adapter changes
  - ZeroClaw adapter integration
  - capability-aware CLI/API filtering
  - tests for plugin/in-tree adapter conformance
- Assign complex adapter/runtime work to `codex` and straightforward tests/docs/conformance checks to `opencode`.

## Acceptance criteria
- A design memo exists for the adapter contract and the ZeroClaw validation target.
- Downstream tickets exist for capability-model work and the ZeroClaw integration path.
- Generated tickets are honest about any missing ZeroClaw upstream primitives and do not push transcript ownership into the orchestrator as a workaround.
- The downstream ticket set preserves richer runtime capabilities instead of flattening adapters to the lowest common denominator.

## Tests
- Verify the generated tickets cover both built-in harness changes and ZeroClaw-specific work.
- Verify generated tickets include capability-discovery behavior that CAR CLI can use to auto-filter features.
- Verify no generated ticket scopes support for single-session runtimes in this build.

## Outcome

The required runtime-thread contract locked by this ticket is:
- create a durable thread/session
- attach/resume a durable thread/session
- send a message
- report execution status until terminal
- provide terminal assistant plain-text output
- surface capability metadata

The strongest contract adjustments identified from current CAR code are:
- expand `AgentCapability` beyond the current coarse flags
- make interrupt capability-gated instead of implicitly universal
- add explicit support for status reporting and optional plain-text transcript retrieval
- keep `AgentHarness` as the one runtime seam instead of inventing a second abstraction

The ZeroClaw integration outcome from this scope is:
- validate ZeroClaw through the same generalized thread-target path used by built-in adapters
- use a CAR-owned wrapper or upstream extension if a required public session primitive is missing
- do not treat CAR transcript mirrors as canonical conversation state
