# Agent Onboarding

This is the minimal conceptual model for any agent operating in the CAR ecosystem (either working on CAR or running inside CAR). Interpret “state” and “artifacts” relative to your execution context.

## Core rules
- **Files are truth**. Durable artifacts > chat > model memory.
- **Reload reality** before acting. Do not trust previous messages.
- **YOLO by default**. Full permissions under assumed isolated workspaces; safety is opt-in posture.
- **Leave evidence**. Every action must be explainable from artifacts.

## What “load state from disk” means
- Ground decisions in current authoritative files for your context (repo docs/specs OR `.codex-autorunner/` state).
- If it matters, represent it on disk.

## What “inspect logs/current run” means
- Check recent run attempts and their evidence before making changes.
- Decide whether you are continuing, correcting, or starting fresh based on artifacts.

## Convergence behavior
- Prefer small diffs and explicit validation.
- If uncertain, write a clarifying artifact instead of guessing.
