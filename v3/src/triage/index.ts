/**
 * WS-B owns src/triage/: rules pass, coalescer, LLM loop, tools, safety.
 * Scaffold stub: marks trivial events keep-informed, leaves the rest pending
 * for the real engine.
 */
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import type { ActionBus, ChannelPort, MemoryReader, MemoryWriter, PolicyPort, TriagePort } from "../ports.ts";

const TRIVIAL_TYPES = new Set(["heartbeat", "progress", "session.started", "artifact"]);

export interface TriageDeps {
  policy: PolicyPort;
  actions: ActionBus;
  channel: ChannelPort;
  memoryReader: MemoryReader;
  memoryWriter: MemoryWriter;
}

export function createTriage(store: Store, config: CarConfig, _deps: TriageDeps): TriagePort {
  return {
    async tick(): Promise<number> {
      const rows = store.claimPendingEvents(50, config.triage.lease_seconds);
      let handled = 0;
      for (const row of rows) {
        if (TRIVIAL_TYPES.has(row.type)) {
          store.setEventTriageState(row.id, "rules_resolved");
          handled++;
        } else {
          // Real engine (WS-B) coalesces and triages; stub returns to pending.
          store.setEventTriageState(row.id, "pending");
        }
      }
      return handled;
    },
  };
}
