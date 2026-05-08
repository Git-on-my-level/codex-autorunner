/**
 * Mirrors `adapters/chat/model_selection.py` `REASONING_EFFORT_VALUES`.
 * Ticket frontmatter stores the raw effort token; empty string means "use default" (omit on save).
 */
export const PMA_REASONING_EFFORT_VALUES = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'] as const;

export type PmaReasoningEffortValue = (typeof PMA_REASONING_EFFORT_VALUES)[number];

/** `id` fields from GET /hub/pma/agents (see surfaces/web/routes/pma_routes/meta.py). */
export function agentIdsFromPmaAgentsPayload(agents: Record<string, unknown>[]): string[] {
  const ids = agents
    .map((row) => (typeof row.id === 'string' ? row.id.trim().toLowerCase() : ''))
    .filter((id): id is string => Boolean(id));
  return [...new Set(ids)].sort((a, b) => a.localeCompare(b));
}
