# Hermes in CAR

CAR uses Hermes through the ACP-style runtime path. Hermes support should stay
behind the same protocol boundaries as Codex and OpenCode: runtime-specific code
belongs in the agent adapter, while surfaces consume canonical CAR turn events.

For the current architecture and caveats, see:

- [Hermes ACP operations](../ops/hermes-acp.md)
- [Hermes ACP architecture](../architecture/hermes-acp-v1.md)
