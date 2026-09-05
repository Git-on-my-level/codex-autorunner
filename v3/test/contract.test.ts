import { describe, expect, test } from "bun:test";
import { CONTRACT_VERSION, computedIdempotencyKey, parseEvent } from "../src/contract/events.ts";
import { humanFactId, payloadSha256, stableJson, ulid } from "../src/contract/ids.ts";
import {
  EffectProposal,
  EffectState,
  EffectTerminalOutcome,
  GrantStatus,
  InteractionState,
  OutboxState,
  ProviderEvent,
  ProviderEventType,
} from "../src/contract/lifecycle.ts";

const valid = {
  contract: CONTRACT_VERSION,
  idempotency_key: "claude-code:sess-abc:PermissionRequest:toolu_01X",
  ts: "2026-08-26T18:04:11Z",
  source: { vendor: "claude-code", host: "davids-mbp", adapter: "hook-http" },
  session: {
    vendor: "claude-code",
    native_id: "sess-abc123",
    host: "davids-mbp",
    cwd: "/Users/dazheng/omi",
  },
  type: "attention.permission",
  severity: "attention",
  requires_response: true,
  response_channel: { kind: "claude-hook-http", hint: { tool_use_id: "toolu_01X" } },
  title: "Permission: gh pr merge 42",
  payload: { tool_name: "Bash" },
};

describe("car.event.v1", () => {
  test("parses a valid event and applies defaults", () => {
    const ev = parseEvent(valid);
    expect(ev.body).toBe("");
    expect(ev.severity).toBe("attention");
    expect(ev.session?.repo_verified).toBe(false);
  });

  test("minimal sessionless event", () => {
    const ev = parseEvent({
      contract: CONTRACT_VERSION,
      idempotency_key: "k1",
      ts: "2026-08-26T18:04:11Z",
      source: { vendor: "cron", host: "h", adapter: "curl" },
      type: "note",
    });
    expect(ev.session).toBeNull();
    expect(ev.requires_response).toBe(false);
  });

  test("rejects unknown type and missing idempotency key", () => {
    expect(() => parseEvent({ ...valid, type: "bogus" })).toThrow();
    expect(() => parseEvent({ ...valid, idempotency_key: undefined })).toThrow();
  });

  test("computed idempotency key is stable within a minute bucket", () => {
    const a = computedIdempotencyKey("cron", "note", { x: 1 }, new Date("2026-08-26T18:04:11Z"));
    const b = computedIdempotencyKey("cron", "note", { x: 1 }, new Date("2026-08-26T18:04:59Z"));
    const c = computedIdempotencyKey("cron", "note", { x: 1 }, new Date("2026-08-26T18:05:01Z"));
    expect(a).toBe(b);
    expect(a).not.toBe(c);
  });

  test("ulid is monotonic within a timestamp", () => {
    const ids = Array.from({ length: 100 }, () => ulid(1700000000000));
    const sorted = [...ids].sort();
    expect(ids).toEqual(sorted);
  });

  test("canonical hashes do not depend on object insertion order", () => {
    expect(stableJson({ b: 2, a: 1 })).toBe(stableJson({ a: 1, b: 2 }));
    expect(payloadSha256({ b: 2, a: 1 })).toBe(payloadSha256({ a: 1, b: 2 }));
    expect(humanFactId()).toStartWith("fact_");
  });

  test("provider and effect contracts use closed lifecycle vocabularies", () => {
    const proposal = EffectProposal.parse({
      contract: "car.effect-proposal.v1",
      intent_id: "intent-1",
      effect_type: "notify",
      args: { text: "hello" },
      lineage_id: "lineage-1",
    });
    expect(proposal.effect_type).toBe("notify");
    expect(() => EffectProposal.parse({ ...proposal, effect_type: "shell" })).toThrow();
    expect(EffectState.options).toContain("terminal_recorded");
    expect(EffectTerminalOutcome.options).toContain("uncertain");
    expect(GrantStatus.options).toContain("revoked");
    expect(InteractionState.options).toContain("consumed");
    expect(OutboxState.options).toContain("uncertain");
  });

  test("provider event contract preserves typed sequence semantics", () => {
    const event = ProviderEvent.parse({
      contract: "car.provider-event.v1",
      invocation_id: "pinv-1",
      request_id: "req-1",
      provider_id: "hermes",
      provider_instance: "hermes:work",
      type: "progress_snapshot",
      seq: 2,
      ts: "2026-08-26T18:04:11Z",
      payload: { text: "cumulative" },
    });
    expect(event.type).toBe("progress_snapshot");
    expect(ProviderEventType.options).not.toContain("unknown");
    expect(() => ProviderEvent.parse({ ...event, type: "done" })).toThrow();
  });
});
