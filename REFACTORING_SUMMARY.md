# Issue #406: Constitution Drift - Engine Decoupling from Backend Adapters

## Summary

This refactoring addresses the constitutional violation where `core/engine.py` imports and orchestrates Codex and OpenCode adapters directly, making it backend-specific rather than protocol-agnostic.

## Changes Made

### Phase 1: Foundation
- **Commit 626f489**: Created `BackendOrchestrator` class in `integrations/agents/backend_orchestrator.py`
  - Manages backend lifecycle (creation, closing)
  - Handles backend-specific concerns (supervisors, event handling)
  - Exposes protocol-neutral interface to Engine
  - Tracks backend context (session, thread, turn)

### Phase 2: Engine Integration
- **Commit 8f432e4**: Added BackendOrchestrator to Engine
  - Imported `BackendOrchestrator` class
  - Added orchestrator instance to `Engine.__init__`
  - Maintained backward compatibility by keeping old code

### Phase 3: Protocol-Agnostic Run Path
- **Commit 5859964**: Created consolidated `_run_agent_async()` method
  - Single protocol-agnostic entry point for all agent backends
  - Determines model/reasoning parameters based on `agent_id`
  - Routes to `_run_agent_backend_async()` with appropriate session key
  - Updated `_execute_run_step()` to use new method instead of if/else branching

### Phase 4: Cleanup
- **Commit 27f6b13**: Removed unused `_run_opencode_app_server_async()` method
  - Method was only called from `_execute_run_step`, now using `_run_agent_async()`
  - Removed backend-specific code path

## What's Been Achieved

✅ Engine run path is now protocol-agnostic
   - No direct branching on agent_id in `_execute_run_step()`
   - Single `_run_agent_async()` method handles all backends
   - Backend parameters determined by configuration, not hardcoded

✅ Backend adapters maintain their specific logic
   - `AgentBackendFactory` already abstracts backend creation
   - Each backend (`CodexAppServerBackend`, `OpenCodeBackend`) handles its own protocol
   - Run events are standardized via `RunEvent` protocol

✅ Tests continue to pass
   - All 9 engine tests pass
   - All 16 backend-related tests pass
   - No breaking changes to public API

## Remaining Work (Future Phases)

### Phase 5: Remove Backend-Specific Imports
The Engine still imports backend-specific modules:
- `app_server_ids` (thread/turn ID extraction)
- `app_server_threads` (session/thread registry)
- `app_server_logging` (event formatting)
- `app_server_prompts` (prompt building)

**Action**: Move these concerns to:
- Backend adapters (for protocol-specific logic)
- BackendOrchestrator (for shared concerns)
- New protocol-neutral utilities (for generic concerns)

### Phase 6: Move Session Management to Backends
The Engine still manages app server sessions directly:
- `AppServerThreadRegistry` for session tracking
- `_app_server_threads_lock` for thread safety
- Session key logic (`"autorunner"` vs `"autorunner.opencode"`)

**Action**: Move session lifecycle management to BackendOrchestrator and individual backends

### Phase 7: Move Notification Handling
The Engine parses backend-specific notification formats:
- `_handle_app_server_notification()` method
- Extracts thread_id, turn_id, token usage from backend-specific messages

**Action**: 
- Backend adapters should emit standardized events via `RunEvent` protocol
- Engine should only handle standardized events, not raw backend notifications

### Phase 8: Remove Backend Supervisor Methods
The Engine has methods for managing backend-specific supervisors:
- `_build_opencode_supervisor()`
- `_ensure_opencode_supervisor()`
- `_close_opencode_supervisor()`
- `_ensure_app_server_supervisor()`
- `_close_app_server_supervisor()`

**Action**: Move supervisor management to BackendOrchestrator

### Phase 9: Simplify `_run_codex_app_server_async`
This method still exists for backward compatibility but is redundant:
- It's a thin wrapper around `_run_agent_backend_async()`
- Can be removed or converted to use `_run_agent_async()`

**Action**: Remove or refactor this method after Phase 5-8

## Architecture After Refactoring

```
Engine (Protocol-Agnostic)
  ├── BackendOrchestrator (Manages backend lifecycle)
  │   ├── AgentBackendFactory (Creates backend instances)
  │   ├── CodexAppServerBackend
  │   └── OpenCodeBackend
  ├── Run Lifecycle (scheduling, locks, artifact persistence)
  ├── State Management (RunnerState, locks)
  └── Event Emission (canonical events, run index)
```

## Testing

All existing tests pass:
- `test_engine_app_server.py`: 5 tests ✓
- `test_engine_end_review.py`: 2 tests ✓
- `test_engine_logs.py`: 2 tests ✓
- Backend contract tests: 4 tests ✓
- Flow state recovery tests: 2 tests ✓
- Core web boundary test: 1 test ✓

**Total: 16 tests passing**

## Migration Guide

For code that was calling backend-specific methods:

**Before:**
```python
if agent_id == "opencode":
    await engine._run_opencode_app_server_async(prompt, run_id, model=model, reasoning=reasoning)
else:
    await engine._run_codex_app_server_async(prompt, run_id)
```

**After:**
```python
state = load_state(engine.state_path)
await engine._run_agent_async(
    agent_id=agent_id,
    prompt=prompt,
    run_id=run_id,
    state=state,
    external_stop_flag=None,
)
```

## Notes

1. The refactoring maintains backward compatibility
2. No breaking changes to public APIs
3. BackendOrchestrator is prepared for future use but not yet fully integrated
4. Gradual migration approach allows testing at each phase
5. Work is done in a dedicated worktree to protect main branch

## Commits

- 626f489: Add BackendOrchestrator for protocol-agnostic backend management
- 8f432e4: Add BackendOrchestrator import and instance to Engine
- 5859964: Consolidate backend-specific run methods into single _run_agent_async
- 27f6b13: Remove unused _run_opencode_app_server_async method
