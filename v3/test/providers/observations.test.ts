import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CONTRACT_VERSION, parseEvent } from "../../src/contract/events.ts";
import { createProviderHost } from "../../src/providers/host.ts";
import { createProviderObservationLoop } from "../../src/providers/observations.ts";
import { testConfig, memoryStore } from "../fakes.ts";

describe("provider observation loop", () => {
  test("delivers a durable fact once and records a terminal provider invocation", async () => {
    const dir = mkdtempSync(join(tmpdir(), "car-provider-observe-"));
    try {
      const config = testConfig({ state_dir: dir });
      const store = memoryStore();
      const event = store.ingestEvent(parseEvent({
        contract: CONTRACT_VERSION,
        idempotency_key: "observe-event",
        ts: "2026-08-26T12:00:00Z",
        source: { vendor: "other", host: "test-host", adapter: "test" },
        session: null,
        type: "note",
        title: "observe me",
      }));
      const interaction = store.recordInteraction({
        sourceId: "telegram:user-1",
        idempotencyKey: "feedback-1",
        kind: "feedback",
        targetType: "event",
        targetId: event.event_id,
        actorId: "david",
        body: { verdict: "confirmed" },
      });
      const host = createProviderHost(store, config);
      const loop = createProviderObservationLoop({
        store,
        config,
        registry: host.registry,
        ensureProvider: (resolved, capability) => host.ensure(resolved, capability),
      });

      expect(await loop.tick()).toBe(1);
      expect(await loop.tick()).toBe(0);
      expect(store.db.query("SELECT state FROM interactions WHERE id = ?").get(interaction.interactionId)).toEqual({ state: "consumed" });
      expect(store.db.query("SELECT capability, state, terminal_outcome FROM provider_invocations").get()).toEqual({
        capability: "memory.observe",
        state: "terminal_recorded",
        terminal_outcome: "succeeded",
      });
      await host.close();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
