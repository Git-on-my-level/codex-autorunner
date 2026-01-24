# Engineering Standards

Purpose: keep the codebase evolvable under heavy agent contribution while optimizing for speed.

## Change hygiene
- One primary intent per diff.
- Separate mechanical refactors from behavioral changes.
- Prefer local changes over cross-cutting rewrites.

## Config discipline
- Behavior-changing defaults must be explicit and discoverable.
- Prefer file-backed config over env-only hidden state.
- Treat config changes like code: reviewable and versioned.

## Generated artifacts
- Generated outputs must be reproducible and clearly marked.
- Avoid hand-editing generated files; change generators instead.

## Tests (80/20)
- Optimize for invariant-locking and regression prevention on core flows.
- Prefer golden-path tests over exhaustive matrices.
- Avoid heavyweight test harnesses that slow iteration.

## Documentation discipline
- Docs explain **why**; code explains **how**.
- Update docs when changing invariants, workflows, or agent behavior.
- Prefer stable mental models over file-path instructions.

## Failure handling
- No silent fallbacks.
- Errors must be attributable to a run and leave evidence.
- Timeouts and partial outputs are first-class outcomes.
