# Communication
- After completing tasks, provide brief results summary rather than verbose reiteration. Confidence: 0.85
- Do not reiterate or summarize subagent results unless explicitly asked; those results are already visible. Confidence: 0.85

# Debugging
- Root cause issues systematically; do not apply bandaid fixes or flakey fallback logic. Fix the source, not the symptom. Confidence: 0.80

# UI architecture
- When building new UI features, reference the old/legacy UI for design patterns, behavior, and affordances; the old UI is the interaction reference. Confidence: 0.75
- Dogfood the app (test it yourself end-to-end) to uncover issues before asking the user for manual testing feedback. Confidence: 0.70

# Workflow
- For complex multi-faceted tasks, parallelize work using subagents (implementer + reviewer interleaved waves). Confidence: 0.80
- Follow the sequence: review changes → commit → push (rebase if needed). Confidence: 0.80
- Before implementation on complex changes, write a plan first for user review; prefer clean "platonic ideal" architecture over incremental hacks. Confidence: 0.80
- Commit between each cleanup/fix wave before starting the next wave; this keeps failures bisectable. Confidence: 0.80

# Code-style
- Always ensure pre-commit hooks pass before finalizing a commit; run relevant checks locally before committing. Confidence: 0.75

# Architecture
- Never mix repo data with worktree data; repo views must not include or mutate worktree state. Confidence: 0.85
- Prefer clean removal of dormant abstractions over keeping unused skeletons; if no production code uses it, delete it. Confidence: 0.80
- No backwards-compat shims or legacy fallback paths; do clean migrations only, error or warn on old shapes. Confidence: 0.85
- Backend is the source of truth for state; UI should be a thin display layer, not invent significant state independently. Confidence: 0.70

# UI components
- Standardize shared components; prefer one canonical source of truth over multiple divergent implementations of the same behavior. Confidence: 0.70
