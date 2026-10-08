/**
 * Mutating operations reachable from the web UI. Every one of these either goes
 * through a documented Store/MemoryWriter method, or is a direct UPDATE paired
 * with an explicit store.audit() call (mirroring src/memory/index.ts's own
 * insert() pattern) — never a bare write.
 */
import type { Store } from "../../store/db.ts";
import type { MemoryWriter } from "../../ports.ts";
import { getMemory } from "./queries.ts";

const AUTONOMY_DEMOTE: Record<string, string> = {
  granted: "suggest",
  suggest: "none",
  none: "none",
};

export type MemoryActionResult = { ok: true } | { ok: false; reason: string };

/** granted -> suggest -> none. Never touches promotion (that stays a Telegram-tap flow). */
export function demoteMemory(store: Store, writer: MemoryWriter, id: string): MemoryActionResult {
  const row = getMemory(store.db, id);
  if (!row) return { ok: false, reason: "not_found" };
  const next = AUTONOMY_DEMOTE[row.autonomy] ?? "none";
  writer.setAutonomy(id, next as "none" | "suggest" | "granted", "david");
  return { ok: true };
}

export function archiveMemory(store: Store, id: string): MemoryActionResult {
  const row = getMemory(store.db, id);
  if (!row) return { ok: false, reason: "not_found" };
  const now = store.clock.now().toISOString();
  store.db.query("UPDATE memories SET status = 'archived', updated_at = ? WHERE id = ?").run(now, id);
  store.audit("david", "memory.archived", "memory", id, { previous_status: row.status });
  return { ok: true };
}

export function decideProposal(store: Store, id: string, decision: "approve" | "reject"): MemoryActionResult {
  const row = getMemory(store.db, id);
  if (!row) return { ok: false, reason: "not_found" };
  if (row.status !== "pending") return { ok: false, reason: "not_pending" };
  const status = decision === "approve" ? "active" : "archived";
  const verb = decision === "approve" ? "memory.proposal_approved" : "memory.proposal_rejected";
  const now = store.clock.now().toISOString();
  store.db.query("UPDATE memories SET status = ?, updated_at = ? WHERE id = ?").run(status, now, id);
  store.audit("david", verb, "memory", id, {});
  return { ok: true };
}

export function addNote(
  writer: MemoryWriter,
  text: string,
  scope: { vendor?: string; repo?: string },
): string {
  const cleanScope: Record<string, unknown> = {};
  if (scope.vendor) cleanScope.vendor = scope.vendor;
  if (scope.repo) cleanScope.repo = scope.repo;
  return writer.addFromDavid("note", "fact", { text }, cleanScope);
}
