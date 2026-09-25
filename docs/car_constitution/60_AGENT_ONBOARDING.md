# Agent Onboarding

This is the minimal conceptual model for any agent operating in the CAR ecosystem (either working on CAR or running inside CAR). Interpret “state” and “artifacts” relative to your execution context.

## Core rules
- **Durable core facts are truth**. Core state and artifacts > chat > provider/model memory.
- **Reload reality** before acting. Do not trust previous messages.
- **Autonomy is granted**. A provider suggestion or remembered preference is not
  authority; every autonomous v3 effect needs a matching human grant and core safety
  approval.
- **Providers propose; CAR executes**. Do not bypass the typed effect path.
- **Leave evidence**. Every action must be explainable from artifacts.

## What “load state from disk” means
- Ground decisions in current authoritative state for your context: v3 core SQLite and
  `~/.car/` configuration, provider-owned state only for provider internals, or legacy
  `.codex-autorunner/` state when explicitly maintaining deprecated v2.
- If it matters, represent it on disk.

## What “inspect logs/current run” means
- Check recent run attempts and their evidence before making changes.
- Decide whether you are continuing, correcting, or starting fresh based on artifacts.

## Convergence behavior
- Prefer small diffs and explicit validation.
- If uncertain, write a clarifying artifact instead of guessing.
- Provider absence, timeout, or ambiguous terminal state falls back to human escalation;
  do not infer success from silence or provider-private files.
