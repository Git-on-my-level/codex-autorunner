# Chat Integration Layer

- `integrations/chat/` owns shared chat-surface kernels, contracts, and lifecycle helpers.
- Keep command identity and workspace semantics in shared command modules (`command_contract.py`, `command_kernel.py`), not in Discord or Telegram transport files.
- Keep managed-thread coordinator setup, durable final-delivery hooks, and bound queue-progress wiring in shared chat modules when the behavior is intentionally surface-parity.
- Discord and Telegram modules should own only transport adapters: ingress payload parsing, platform IDs, message formatting, and API delivery calls.
- When extracting behavior from a surface file, prefer adding a reusable shared helper here only if both surfaces can consume the same contract without platform conditionals leaking back in.
