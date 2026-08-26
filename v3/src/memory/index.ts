/**
 * WS-C owns src/memory/: charter + rule/note/episode tiers, outcome recorder,
 * promotion loop, nightly consolidation, `card memory` support.
 * Scaffold stub: empty reads, direct writes with audit; no consolidation.
 */
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import { charterPath } from "../config/config.ts";
import type { MemoryHit, MemoryReader, MemoryWriter } from "../ports.ts";
import { memoryId, outcomeId } from "../contract/ids.ts";

export function createMemory(
  store: Store,
  config: CarConfig,
): { reader: MemoryReader; writer: MemoryWriter; consolidationJob: () => Promise<void> } {
  const readCharter = (): string => {
    try {
      return require("node:fs").readFileSync(charterPath(config), "utf8") as string;
    } catch {
      return "";
    }
  };

  const insert = (
    tier: string,
    kind: string,
    content: Record<string, unknown>,
    scope: Record<string, unknown>,
    authoredBy: string,
    status: string,
  ): string => {
    const id = memoryId();
    const now = store.clock.now().toISOString();
    store.db
      .query(
        `INSERT INTO memories (id, tier, scope_json, kind, content_json, authored_by, status, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(id, tier, JSON.stringify(scope), kind, JSON.stringify(content), authoredBy, status, now, now);
    store.db
      .query("INSERT INTO memories_fts (memory_id, content, scope_text) VALUES (?, ?, ?)")
      .run(id, JSON.stringify(content), JSON.stringify(scope));
    store.audit(authoredBy, "memory.created", "memory", id, { tier, kind, status });
    return id;
  };

  const reader: MemoryReader = {
    assembleContext(_input) {
      return { charter: readCharter(), hits: [] };
    },
    search(_query, _scope): MemoryHit[] {
      return [];
    },
    get(_id): MemoryHit | null {
      return null;
    },
    grantedRules(_input): MemoryHit[] {
      return [];
    },
  };

  const writer: MemoryWriter = {
    addFromDavid(tier, kind, content, scope) {
      return insert(tier, kind, content, scope, "david", "active");
    },
    propose(kind, content, scope) {
      return insert(kind === "fact" ? "note" : "rule", kind, content, scope, "triage", "pending");
    },
    recordOutcome(input) {
      const id = outcomeId();
      store.db
        .query(
          "INSERT INTO outcomes (id, decision_id, escalation_id, verdict, david_action_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        )
        .run(
          id,
          input.decisionId,
          input.escalationId ?? null,
          input.verdict,
          input.davidAction ? JSON.stringify(input.davidAction) : null,
          store.clock.now().toISOString(),
        );
      store.audit("outcome", "outcome.recorded", "outcome", id, { verdict: input.verdict });
    },
    setAutonomy(memoryIdArg, autonomy, by) {
      store.db
        .query("UPDATE memories SET autonomy = ?, updated_at = ? WHERE id = ?")
        .run(autonomy, store.clock.now().toISOString(), memoryIdArg);
      store.audit(by, "memory.autonomy_set", "memory", memoryIdArg, { autonomy });
    },
  };

  return { reader, writer, consolidationJob: async () => {} };
}
