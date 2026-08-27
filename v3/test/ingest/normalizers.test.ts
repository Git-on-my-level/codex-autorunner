/**
 * Normalizer edge cases that no realistic golden fixture should carry:
 * truncation, degraded/unknown vendor vocabularies, and the contract clamps.
 */
import { describe, expect, test } from "bun:test";
import { MAX_BODY_BYTES, MAX_PAYLOAD_BYTES } from "../../src/contract/events.ts";
import { normalizeAgentctl, unwrapAgentctlDelivery } from "../../src/ingest/agentctl.ts";
import { normalizeClaude } from "../../src/ingest/claude.ts";
import { normalizeMultica } from "../../src/ingest/multica.ts";
import { buildEvent, makeContext, NormalizeError } from "../../src/ingest/normalize.ts";
import { FIXTURE_HOST, FIXTURE_NOW } from "./fixtures.ts";

const ctx = () => makeContext(FIXTURE_NOW, FIXTURE_HOST);

function journalEvent(overrides: Record<string, unknown> = {}) {
  return {
    id: "event-test-one",
    origin_host_id: "host-test",
    execution_id: "exec-test",
    sequence: 1,
    ordering: "source",
    kind: "progress",
    state: "running",
    adapter: "codex",
    observed_at: "2026-08-26T12:00:00Z",
    payload: {},
    ...overrides,
  };
}

describe("contract clamps", () => {
  test("an oversize body is truncated with a marker instead of failing validation", () => {
    const event = buildEvent({
      idempotency_key: "clamp:1",
      ts: FIXTURE_NOW.toISOString(),
      vendor: "cron",
      adapter: "test",
      host: FIXTURE_HOST,
      session: null,
      type: "note",
      severity: "info",
      body: "x".repeat(MAX_BODY_BYTES * 2),
    });
    expect(event.body.length).toBeLessThanOrEqual(MAX_BODY_BYTES);
    expect(event.body).toEndWith("[truncated by CAR ingest]");
  });

  test("an oversize payload is replaced by a self-describing marker", () => {
    const event = buildEvent({
      idempotency_key: "clamp:2",
      ts: FIXTURE_NOW.toISOString(),
      vendor: "cron",
      adapter: "test",
      host: FIXTURE_HOST,
      session: null,
      type: "note",
      severity: "info",
      payload: { blob: "y".repeat(MAX_PAYLOAD_BYTES * 2) },
    });
    expect(event.payload["_car_truncated"]).toBe(true);
    expect(event.payload["_car_original_bytes"]).toBeGreaterThan(MAX_PAYLOAD_BYTES);
    expect(String(event.payload["_car_preview"]).length).toBeLessThanOrEqual(2048);
  });

  test("an overlong title is clamped rather than rejected", () => {
    const event = buildEvent({
      idempotency_key: "clamp:3",
      ts: FIXTURE_NOW.toISOString(),
      vendor: "cron",
      adapter: "test",
      host: FIXTURE_HOST,
      session: null,
      type: "note",
      severity: "info",
      title: "t".repeat(2000),
    });
    expect(event.title.length).toBe(512);
    expect(event.title).toEndWith("…");
  });
});

describe("agentctl normalizer", () => {
  test("unwrapping accepts bare events, arrays and envelopes", () => {
    expect(unwrapAgentctlDelivery(journalEvent()).events).toHaveLength(1);
    expect(unwrapAgentctlDelivery([journalEvent(), journalEvent()]).events).toHaveLength(2);

    const enveloped = unwrapAgentctlDelivery({
      delivery_id: "d1",
      subscription_id: "sub-1",
      attempt: 2,
      events: [journalEvent()],
    });
    expect(enveloped.events).toHaveLength(1);
    expect(enveloped.delivery).toEqual({ delivery_id: "d1", subscription_id: "sub-1", attempt: 2 });
  });

  test("a singular `event` key is unwrapped too", () => {
    expect(unwrapAgentctlDelivery({ delivery_id: "d", event: journalEvent() }).events).toHaveLength(1);
  });

  test("delivery metadata is preserved on the canonical payload", () => {
    const [event] = normalizeAgentctl({ delivery_id: "d1", events: [journalEvent()] }, ctx());
    expect((event!.payload as any).delivery).toMatchObject({ delivery_id: "d1" });
  });

  test("an unusable payload raises NormalizeError", () => {
    expect(() => normalizeAgentctl({ nothing: true }, ctx())).toThrow(NormalizeError);
    expect(() => normalizeAgentctl("nope", ctx())).toThrow(NormalizeError);
    expect(() => normalizeAgentctl([{ kind: "progress" }], ctx())).toThrow(/execution_id/);
  });

  test("attention flavours are discriminated, defaulting to a question", () => {
    const flavour = (payload: Record<string, unknown>, sourceState?: string) =>
      normalizeAgentctl(
        journalEvent({ kind: "attention", payload, ...(sourceState ? { source_state: sourceState } : {}) }),
        ctx(),
      )[0]!;

    expect(flavour({ attention_kind: "permission" }).type).toBe("attention.permission");
    expect(flavour({}, "approval_requested").type).toBe("attention.permission");
    expect(flavour({ reason: "waiting_for_input" }).type).toBe("attention.idle");
    expect(flavour({ type: "error" }).type).toBe("attention.error");
    expect(flavour({ type: "error" }).severity).toBe("urgent");
    expect(flavour({ question: "which branch?" }).type).toBe("attention.question");
  });

  test("every attention event requires a response and carries an agentctl-run channel", () => {
    const [event] = normalizeAgentctl(journalEvent({ kind: "attention", payload: {} }), ctx());
    expect(event!.requires_response).toBe(true);
    expect(event!.response_channel).toEqual({
      kind: "agentctl-run",
      hint: { execution_id: "exec-test", adapter: "codex" },
    });
  });

  test("every terminal state that is not success raises severity", () => {
    for (const state of ["failed", "cancelled", "timed_out", "orphaned"]) {
      const [event] = normalizeAgentctl(journalEvent({ kind: "terminal", state }), ctx());
      expect(event!.severity).toBe("attention");
    }
    const [ok] = normalizeAgentctl(journalEvent({ kind: "terminal", state: "completed" }), ctx());
    expect(ok!.severity).toBe("info");
  });

  test("an unknown kind degrades to progress instead of being dropped", () => {
    const [event] = normalizeAgentctl(journalEvent({ kind: "some_future_kind" }), ctx());
    expect(event!.type).toBe("progress");
  });

  test("the session ref is always (agentctl, origin host, execution id)", () => {
    const [event] = normalizeAgentctl(journalEvent(), ctx());
    expect(event!.session).toMatchObject({
      vendor: "agentctl",
      host: "host-test",
      native_id: "exec-test",
    });
  });

  test("origin_host_id falls back to the envelope, then to the ambient host", () => {
    const [fromEnvelope] = normalizeAgentctl(
      { origin_host_id: "host-envelope", events: [journalEvent({ origin_host_id: undefined })] },
      ctx(),
    );
    expect(fromEnvelope!.session!.host).toBe("host-envelope");

    const [ambient] = normalizeAgentctl(journalEvent({ origin_host_id: undefined }), ctx());
    expect(ambient!.session!.host).toBe(FIXTURE_HOST);
  });

  test("labels become the session title", () => {
    const [event] = normalizeAgentctl(journalEvent({ labels: ["car-triage", "omi"] }), ctx());
    expect(event!.session!.title).toBe("car-triage omi");
  });
});

describe("claude normalizer", () => {
  test("a missing session_id is rejected", () => {
    expect(() => normalizeClaude({ hook_event_name: "Stop" }, ctx())).toThrow(/session_id/);
    expect(() => normalizeClaude({ session_id: "s" }, ctx())).toThrow(/hook_event_name/);
    expect(() => normalizeClaude("nope", ctx())).toThrow(NormalizeError);
  });

  test("only PermissionRequest asks to be parked", () => {
    const parks = (hook: string) =>
      normalizeClaude({ session_id: "s", hook_event_name: hook, cwd: "/tmp" }, ctx()).park;
    expect(parks("PermissionRequest")).toBe(true);
    for (const hook of ["PreToolUse", "Stop", "SessionStart", "SessionEnd", "Notification"]) {
      expect(parks(hook)).toBe(false);
    }
  });

  test("notification types map onto the closed attention vocabulary", () => {
    const type = (notification_type: string) =>
      normalizeClaude(
        { session_id: "s", hook_event_name: "Notification", notification_type, notification_data: {} },
        ctx(),
      ).event;

    expect(type("permission_prompt").type).toBe("attention.permission");
    expect(type("idle_prompt").type).toBe("attention.idle");
    expect(type("agent_needs_input").type).toBe("attention.question");
    expect(type("elicitation_dialog").type).toBe("attention.question");
    expect(type("agent_completed").type).toBe("progress");
    expect(type("auth_success").type).toBe("note");
  });

  test("a Notification permission prompt never claims the in-band hook channel", () => {
    // Only PermissionRequest can return a decision; claiming otherwise would make
    // WS-E's adapter answer a response nobody is holding open.
    const { event } = normalizeClaude(
      {
        session_id: "s",
        hook_event_name: "Notification",
        notification_type: "permission_prompt",
        notification_data: {},
      },
      ctx(),
    );
    expect(event.response_channel?.kind).toBe("claude-resume");
  });

  test("notification keys bucket by the minute so a later repeat re-fires", () => {
    const at = (iso: string) =>
      normalizeClaude(
        {
          session_id: "s",
          hook_event_name: "Notification",
          notification_type: "idle_prompt",
          notification_data: { message: "waiting" },
        },
        makeContext(new Date(iso), FIXTURE_HOST),
      ).event.idempotency_key;

    expect(at("2026-08-26T12:00:10Z")).toBe(at("2026-08-26T12:00:50Z"));
    expect(at("2026-08-26T12:00:10Z")).not.toBe(at("2026-08-26T13:00:10Z"));
  });

  test("a PermissionRequest without a tool_use_id still gets a stable key", () => {
    const key = () =>
      normalizeClaude(
        {
          session_id: "s",
          hook_event_name: "PermissionRequest",
          tool_name: "Bash",
          tool_input: { command: "ls" },
        },
        ctx(),
      ).event.idempotency_key;
    expect(key()).toBe(key());
    expect(key()).toStartWith("claude-code:s:PermissionRequest:Bash:");
  });

  test("the session ref uses the ambient host, since hooks carry none", () => {
    const { event } = normalizeClaude(
      { session_id: "s", hook_event_name: "SessionStart", cwd: "/Users/dazheng/omi" },
      ctx(),
    );
    expect(event.session).toMatchObject({ vendor: "claude-code", host: FIXTURE_HOST, native_id: "s" });
    expect(event.session!.cwd).toBe("/Users/dazheng/omi");
    expect(event.session!.title).toBe("omi");
  });

  /*
   * Every repo-scoped memory matches on session.repo, and scopeMatches treats an
   * empty repo as "no match" — so a Claude session without one silently opts out
   * of repo-scoped rules, notes and the "this repo only" grant.
   */
  test("the session ref carries a repo derived from cwd", () => {
    const { event } = normalizeClaude(
      { session_id: "s", hook_event_name: "SessionStart", cwd: "/Users/dazheng/car-workspace/codex-autorunner" },
      ctx(),
    );
    expect(event.session!.repo).toBe("codex-autorunner");
  });

  test("a session with no cwd reports no repo rather than a guess", () => {
    const { event } = normalizeClaude({ session_id: "s", hook_event_name: "SessionStart" }, ctx());
    expect(event.session!.repo).toBeUndefined();
  });
});

describe("multica normalizer", () => {
  test("an issue reference is mandatory", () => {
    expect(() => normalizeMultica({ action: "updated" }, ctx())).toThrow(/issue reference/);
    expect(() => normalizeMultica("nope", ctx())).toThrow(NormalizeError);
  });

  test("action vocabularies map onto the contract", () => {
    const type = (action: string) => normalizeMultica({ action, issue: { ref: "M-1" } }, ctx()).type;
    expect(type("question")).toBe("attention.question");
    expect(type("assigned")).toBe("attention.question");
    expect(type("review_requested")).toBe("attention.question");
    expect(type("blocked")).toBe("attention.question");
    expect(type("failed")).toBe("attention.error");
    expect(type("closed")).toBe("attention.cleared");
    expect(type("updated")).toBe("note");
  });

  test("a comment ending in a question mark escalates; otherwise it is a note", () => {
    const asQuestion = normalizeMultica(
      { action: "comment_created", issue: { ref: "M-2", comment: { body: "which branch?" } } },
      ctx(),
    );
    expect(asQuestion.type).toBe("attention.question");
    expect(asQuestion.requires_response).toBe(true);

    const asNote = normalizeMultica(
      { action: "comment_created", issue: { ref: "M-2", comment: { body: "shipped." } } },
      ctx(),
    );
    expect(asNote.type).toBe("note");
    expect(asNote.requires_response).toBe(false);
  });

  test("the issue ref is always in the response-channel hint", () => {
    const event = normalizeMultica({ action: "question", issue: { ref: "M-9" } }, ctx());
    expect(event.response_channel).toMatchObject({ kind: "multica-api", hint: { issue: "M-9" } });
    expect(event.session).toMatchObject({ vendor: "multica", native_id: "M-9" });
  });

  test("a numeric issue id is accepted", () => {
    const event = normalizeMultica({ action: "question", issue: { number: 128 } }, ctx());
    expect(event.session!.native_id).toBe("128");
  });
});
