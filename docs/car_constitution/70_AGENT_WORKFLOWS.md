# Agent Workflows (high level)

Goal: multi-step work converges via durable artifacts, not chat memory.

## Phases (conceptual)
1) Context acquisition
2) Plan articulation (only when needed)
3) Execution
4) Validation
5) Finalization

## Phase discipline
- Each phase transition leaves an artifact (notes, diffs, outputs, run events).
- If a run stops mid-phase, another agent can resume from artifacts alone.

## Parallelism
- Parallel work is allowed, but convergence happens by editing shared artifacts.
- If two agents disagree, resolve by updating the same durable plan/decision artifact.

## Human-in-the-loop
- Human decisions are authoritative and must be recorded to disk.
- Agents may block awaiting a human artifact when needed.

## Recovery
- Any phase can be retried from artifacts alone.
- Prefer “cheap reset” (workspace recreation) over complex cleanup.
