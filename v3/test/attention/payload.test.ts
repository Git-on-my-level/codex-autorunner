import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AttentionClient } from "../../src/attention/client.ts";
import { recordAnswer, recordSessionReply } from "../../src/attention/replies.ts";
import { AttentionService } from "../../src/attention/service.ts";
import { FakeChannel, FakeClock, memoryStore, testConfig } from "../fakes.ts";
import { DecisionPacket, type ClientIdentity } from "../../src/attention/contract.ts";

const owner: ClientIdentity = { workspaceId: "payload-test", clientId: "agent", host: "host-a" };
const packet = DecisionPacket.parse({
  goal: "Ship the change", blocker: "The answer is human-owned", question: "Proceed?",
  why_human: "No existing authority answers this", attempts: ["Checked the current policy"],
  facts: [{ statement: "The change is scoped", source: "test" }],
  recommendation: { answer: "Proceed", rationale: "The evidence supports it" }, impact: "Release is waiting",
  options: [{ id: "yes", label: "Proceed", answer: "Proceed", consequences: "The release continues" }],
});

function guidedFixture() {
  const store = memoryStore(new FakeClock());
  const config = testConfig({ attention: { workspace_id: owner.workspaceId, clients: {} } });
  const service = new AttentionService(store, config, new FakeChannel());
  const row = service.raise(owner, "payload", packet);
  return { store, service, row };
}

describe("strict reply payload boundary", () => {
  const cleanup: (() => void)[] = [];
  afterEach(() => { for (const clean of cleanup.splice(0).reverse()) clean(); });

  test("recordAnswer accepts exactly one text or approval field", () => {
    for (const payload of [{ text: "Proceed" }, { approval: true }, { approval: false }]) {
      const { store, row } = guidedFixture(); cleanup.push(() => store.db.close());
      expect(recordAnswer(store, { escalationId: row.escalation_id!, actor: "human:test", payload }).payload_json)
        .toBe(JSON.stringify(payload));
    }
  });

  test("recordAnswer rejects ambiguous or extra fields before writing", () => {
    for (const payload of [
      { text: "Proceed", approval: true }, { text: "Proceed", extra: "unexpected" },
      { text: "   " }, { text: 42 }, { approval: "yes" },
    ]) {
      const { store, row } = guidedFixture(); cleanup.push(() => store.db.close());
      expect(() => recordAnswer(store, { escalationId: row.escalation_id!, actor: "human:test", payload: payload as never }))
        .toThrow("one nonempty text answer OR one approval decision");
      expect(store.db.query("SELECT COUNT(*) AS n FROM human_replies").get()).toEqual({ n: 0 });
    }
  });

  test("recordSessionReply applies the same one-of validation", () => {
    const store = memoryStore(new FakeClock()); cleanup.push(() => store.db.close());
    expect(() => recordSessionReply(store, { idempotencyKey: "one", actor: "human:test", carSessionId: "session", channel: null,
      payload: { text: "Proceed", approval: true } as never })).toThrow("one nonempty text answer OR one approval decision");
    expect(() => recordSessionReply(store, { idempotencyKey: "two", actor: "human:test", carSessionId: "session", channel: null,
      payload: { text: "Proceed", extra: true } as never })).toThrow("one nonempty text answer OR one approval decision");
    expect(recordSessionReply(store, { idempotencyKey: "three", actor: "human:test", carSessionId: "session", channel: null,
      payload: { text: "Proceed" } }).state).toBe("pending");
  });

  test("client rejects an ambiguous server answer before local receipt", async () => {
    const previousFetch = globalThis.fetch;
    const spoolDir = mkdtempSync(join(tmpdir(), "car-payload-client-")); cleanup.push(() => rmSync(spoolDir, { recursive: true, force: true }));
    const client = new AttentionClient({ url: "http://127.0.0.1:7171", token: "a".repeat(40), spoolDir });
    globalThis.fetch = (async () => new Response(JSON.stringify({
      contract: "car.request.v1", id: "req_one", revision: 1, state: "answered",
      answer: { id: "reply_one", payload: { text: "Proceed", approval: false }, eligible_for_receipt: true, delivery: "staged" },
      next_action: "receive",
    }), { status: 200, headers: { "content-type": "application/json" } })) as unknown as typeof fetch;
    try {
      await expect(client.get("req_one")).rejects.toMatchObject({ code: "invalid_response" });
    } finally {
      globalThis.fetch = previousFetch;
    }
  });
});
