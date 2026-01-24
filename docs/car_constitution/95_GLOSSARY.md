# Glossary

- **Engine**: protocol-agnostic runtime semantics (runs, scheduling, state transitions).
- **Control plane**: filesystem-backed intent + artifacts; canonical state under `.codex-autorunner/`.
- **Adapter**: protocol translation layer (Telegram/Web/Codex/OpenCode) into engine commands.
- **Surface**: user-facing UX (Telegram chat, web UI, terminal views).
- **Run**: a single execution with a unique identity and durable artifacts.
- **Run event**: structured record of a significant state transition/decision.
- **Artifact**: any durable file that explains intent, action, or output.
- **Workspace**: isolated filesystem scope for a task/run (often disposable).
- **YOLO mode**: default permissive execution posture; safety is opt-in.
