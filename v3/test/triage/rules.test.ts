/**
 * The pure rules pass. These are the $0 decisions that keep the LLM as the
 * exception path — and the two hard guarantees (urgent never meets a model,
 * CAR's own events never open LLM triage) live here.
 */
import { describe, expect, test } from "bun:test";
import type { EventRow } from "../../src/store/db.ts";
import { classifyEvent, sessionEndedOk, severityRank, TRIVIAL_TYPES } from "../../src/triage/rules.ts";
import { grantedRule } from "./harness.ts";

const NOW = new Date("2026-08-26T12:00:00Z");

function row(over: Partial<EventRow> = {}): EventRow {
  return {
    id: "evt_1",
    idempotency_key: "claude-code:sess-1:Question:1",
    car_session_id: "sess_A",
    type: "attention.question",
    severity: "attention",
    ts: "2026-08-26T11:59:50Z",
    received_at: "2026-08-26T11:59:50Z",
    requires_response: 1,
    response_channel_json: null,
    title: "Agent asks: proceed?",
    body: "",
    payload_json: "{}",
    expires_at: null,
    actor: "external",
    triage_state: "coalescing",
    incident_id: null,
    source_vendor: "claude-code",
    source_host: "mac-studio",
    source_adapter: "hook-http",
    ...over,
  };
}

const ctx = (granted = [] as ReturnType<typeof grantedRule>[]) => ({ now: NOW, grantedRules: granted });

describe("trivial types", () => {
  for (const type of [...TRIVIAL_TYPES]) {
    test(`${type} resolves without an incident`, () => {
      expect(classifyEvent(row({ type }), ctx()).kind).toBe("resolved");
    });
  }

  test("trivial types resolve even at attention severity", () => {
    expect(classifyEvent(row({ type: "progress", severity: "attention" }), ctx()).kind).toBe("resolved");
  });
});

describe("urgent", () => {
  test("urgent escalates immediately — never queued for the LLM", () => {
    const out = classifyEvent(row({ severity: "urgent" }), ctx());
    expect(out.kind).toBe("escalate");
    expect(out.reason).toContain("no LLM");
  });

  test("urgent outranks an available granted rule", () => {
    const out = classifyEvent(row({ severity: "urgent", type: "attention.permission" }), ctx([grantedRule()]));
    expect(out.kind).toBe("escalate");
  });

  test("urgent outranks self-event suppression: a page still reaches David", () => {
    const out = classifyEvent(row({ severity: "urgent", actor: "car" }), ctx());
    expect(out.kind).toBe("escalate");
  });

  test("a trivial type at urgent severity is still trivial", () => {
    // heartbeat/progress carry no decision; severity on them is noise.
    expect(classifyEvent(row({ type: "heartbeat", severity: "urgent" }), ctx()).kind).toBe("resolved");
  });
});

describe("self-event suppression", () => {
  test("actor=car never opens LLM triage", () => {
    const out = classifyEvent(row({ actor: "car" }), ctx());
    expect(out.kind).toBe("self_event");
  });

  test("actor=car suppresses even attention.error", () => {
    expect(classifyEvent(row({ actor: "car", type: "attention.error" }), ctx()).kind).toBe("self_event");
  });

  test("actor=car outranks a granted rule (no self-triggering loops)", () => {
    const out = classifyEvent(row({ actor: "car", type: "attention.permission" }), ctx([grantedRule()]));
    expect(out.kind).toBe("self_event");
  });

  test("external actors are unaffected", () => {
    expect(classifyEvent(row({ actor: "external" }), ctx()).kind).toBe("llm");
  });
});

describe("session.ended", () => {
  test("an ok outcome resolves", () => {
    const out = classifyEvent(
      row({ type: "session.ended", severity: "info", payload_json: JSON.stringify({ outcome: "ok" }) }),
      ctx(),
    );
    expect(out.kind).toBe("resolved");
  });

  test("exit_code 0 resolves", () => {
    expect(sessionEndedOk(row({ type: "session.ended", payload_json: '{"exit_code":0}' }))).toBe(true);
  });

  test("a failed outcome goes to the LLM", () => {
    const out = classifyEvent(
      row({ type: "session.ended", payload_json: JSON.stringify({ outcome: "failed" }) }),
      ctx(),
    );
    expect(out.kind).toBe("llm");
  });

  test("no outcome reported falls back to severity", () => {
    expect(sessionEndedOk(row({ type: "session.ended", severity: "info", payload_json: "{}" }))).toBe(true);
    expect(sessionEndedOk(row({ type: "session.ended", severity: "attention", payload_json: "{}" }))).toBe(false);
  });

  test("unparseable payload does not throw", () => {
    expect(() =>
      classifyEvent(row({ type: "session.ended", payload_json: "not json" }), ctx()),
    ).not.toThrow();
  });
});

describe("notes", () => {
  test("a note below attention resolves", () => {
    expect(classifyEvent(row({ type: "note", severity: "info" }), ctx()).kind).toBe("resolved");
    expect(classifyEvent(row({ type: "note", severity: "notice" }), ctx()).kind).toBe("resolved");
  });

  test("a note at attention or above is triaged", () => {
    expect(classifyEvent(row({ type: "note", severity: "attention" }), ctx()).kind).toBe("llm");
    expect(classifyEvent(row({ type: "note", severity: "urgent" }), ctx()).kind).toBe("escalate");
  });
});

describe("granted rules", () => {
  test("attention.* matching a granted rule executes deterministically", () => {
    const out = classifyEvent(row({ type: "attention.permission" }), ctx([grantedRule()]));
    expect(out.kind).toBe("granted");
    if (out.kind === "granted") expect(out.rule.id).toBe("mem_granted_1");
  });

  test("a suggest-only rule does NOT grant autonomy", () => {
    const out = classifyEvent(
      row({ type: "attention.permission" }),
      ctx([grantedRule({ autonomy: "suggest" })]),
    );
    expect(out.kind).toBe("llm");
  });

  test("a pending rule does NOT grant autonomy", () => {
    const out = classifyEvent(
      row({ type: "attention.permission" }),
      ctx([grantedRule({ status: "pending" })]),
    );
    expect(out.kind).toBe("llm");
  });

  test("granted rules only apply to attention.* types", () => {
    const out = classifyEvent(row({ type: "session.ended", payload_json: '{"outcome":"failed"}' }), ctx([grantedRule()]));
    expect(out.kind).toBe("llm");
  });
});

describe("expiry and misc", () => {
  test("an event past expires_at is moot", () => {
    const out = classifyEvent(row({ expires_at: "2026-08-26T11:00:00Z" }), ctx());
    expect(out.kind).toBe("expired");
  });

  test("an unexpired deadline is triaged normally", () => {
    expect(classifyEvent(row({ expires_at: "2026-08-26T13:00:00Z" }), ctx()).kind).toBe("llm");
  });

  test("attention.cleared is housekeeping", () => {
    expect(classifyEvent(row({ type: "attention.cleared" }), ctx()).kind).toBe("resolved");
  });

  test("attention.error and attention.idle reach the LLM", () => {
    expect(classifyEvent(row({ type: "attention.error" }), ctx()).kind).toBe("llm");
    expect(classifyEvent(row({ type: "attention.idle" }), ctx()).kind).toBe("llm");
  });

  test("severityRank orders the contract enum", () => {
    expect(severityRank("info")).toBeLessThan(severityRank("notice"));
    expect(severityRank("notice")).toBeLessThan(severityRank("attention"));
    expect(severityRank("attention")).toBeLessThan(severityRank("urgent"));
    expect(severityRank("garbage")).toBe(0);
  });
});
