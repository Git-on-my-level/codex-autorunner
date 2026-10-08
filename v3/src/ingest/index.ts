/** WS-A public surface: the HTTP loop plus the pure per-source normalizers. */
export { createIngestServer, createIngestApp, type IngestOptions } from "./server.ts";
export { normalizeAgentctl, unwrapAgentctlDelivery, AGENTCTL_ADAPTER } from "./agentctl.ts";
export {
  normalizeClaude,
  hookDecisionBody,
  hookTimeoutHintMs,
  readHookTimeoutHint,
  parkDeadlineMs,
  NO_DECISION_BODY,
  CLAUDE_ADAPTER,
  DEFAULT_HOOK_TIMEOUT_MS,
  PARK_MARGIN_MS,
  type ClaudeNormalizeResult,
  type ClaudeNormalizeOptions,
} from "./claude.ts";
export { normalizeMultica, MULTICA_ADAPTER } from "./multica.ts";
export { eventJsonSchema, SCHEMA_ID } from "./schema.ts";
export { authorize, isLoopback, peerAddress, type SourceId } from "./auth.ts";
export { decodeBody, BatchLineError, BodyError } from "./batch.ts";
export { makeContext, buildEvent, NormalizeError, type NormalizeContext } from "./normalize.ts";
