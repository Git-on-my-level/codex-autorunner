/** Durable delivery of human/core outcome facts to the configured memory provider. */
import type { CarConfig } from "../config/config.ts";
import { resolveProvider, type ProviderSelectionContext, type ResolvedProvider } from "../config/provider_topology.ts";
import { payloadSha256 } from "../contract/ids.ts";
import type { Loop } from "../ports.ts";
import type { ProviderRegistry } from "./registry.ts";
import { failureFromUnknown } from "./errors.ts";
import { HumanOrDecisionOutcomeSchema, type CapabilityProvider } from "./types.ts";
import type { Store } from "../store/db.ts";

interface FactRow {
  id: string;
  interaction_id: string;
  kind: "feedback" | "instruction" | "reply" | "grant_created" | "grant_revoked" | "decision_outcome";
  target_type: string;
  target_id: string;
  body_json: string;
}

interface FactContext {
  incidentId: string | null;
  selection: ProviderSelectionContext;
}

export interface ProviderObservationOptions {
  store: Store;
  config: CarConfig;
  registry: ProviderRegistry;
  ensureProvider: (resolved: ResolvedProvider, capability: "memory") => Promise<CapabilityProvider>;
  intervalMs?: number;
  leaseSeconds?: number;
  limit?: number;
}

export interface ProviderObservationLoop extends Loop {
  tick(): Promise<number>;
}

export function createProviderObservationLoop(options: ProviderObservationOptions): ProviderObservationLoop {
  const { store, config, registry } = options;
  const owner = "provider-observations";
  const leaseSeconds = options.leaseSeconds ?? 120;
  const limit = options.limit ?? 50;
  let timer: ReturnType<typeof setInterval> | null = null;
  let running: Promise<number> | null = null;

  async function tick(): Promise<number> {
    if (running) return running;
    running = (async () => {
      const facts = store.db.query(
        `SELECT h.* FROM human_facts h
         JOIN interactions i ON i.id = h.interaction_id
         WHERE i.state IN ('received', 'acknowledged')
         ORDER BY h.created_at ASC, h.id ASC LIMIT ?`,
      ).all(limit) as FactRow[];
      let observed = 0;
      for (const fact of facts) {
        const context = contextForFact(store, fact);
        let resolved: ResolvedProvider;
        try {
          resolved = resolveProvider(config, "memory", context.selection);
        } catch (error) {
          store.audit("router", "provider.observation_resolution_failed", "human_fact", fact.id, { error: String(error) });
          store.transitionInteraction(fact.interaction_id, "rejected", "provider_resolution_failed");
          continue;
        }
        const requestId = `observe_${payloadSha256({ fact_id: fact.id, provider_instance: resolved.instance_id }).slice(0, 40)}`;
        const created = store.createProviderInvocation({
          incidentId: context.incidentId,
          providerId: resolved.provider_id,
          providerInstance: resolved.instance_id,
          providerVersion: registry.get(resolved.instance_id)?.descriptor.provider_version ?? "unknown",
          capability: "memory.observe",
          requestId,
          requestHash: payloadSha256({ fact_id: fact.id, kind: fact.kind, body_json: fact.body_json }),
        });
        const existing = store.getProviderInvocation(created.id);
        if (existing?.state === "terminal_recorded") {
          store.transitionInteraction(
            fact.interaction_id,
            existing.terminal_outcome === "succeeded" ? "consumed" : "rejected",
            existing.terminal_outcome ?? "provider_terminal",
          );
          continue;
        }
        const claim = store.claimProviderInvocation(created.id, owner, leaseSeconds);
        if (!claim?.claim_token) continue;
        try {
          const provider = await options.ensureProvider(resolved, "memory");
          if (!provider.observe) throw new Error(`provider ${resolved.instance_id} does not support memory observation`);
          const input = HumanOrDecisionOutcomeSchema.parse({
            contract: "car.memory.v1",
            request_id: requestId,
            deadline_at: new Date(store.clock.now().getTime() + leaseSeconds * 1_000).toISOString(),
            provider: provider.descriptor,
            kind: fact.kind,
            fact_id: fact.id,
            body: jsonObject(fact.body_json),
            incident_id: context.incidentId,
          });
          await provider.observe(input);
          store.recordProviderTerminal(created.id, { owner, token: claim.claim_token }, "succeeded", { responseRef: JSON.stringify({ fact_id: fact.id }) });
          store.transitionInteraction(fact.interaction_id, "consumed", "observed");
          observed++;
        } catch (error) {
          const descriptor = registry.get(resolved.instance_id)?.descriptor;
          const failure = descriptor ? failureFromUnknown(error, descriptor, requestId) : { code: "unknown", message: String(error), retryable: false, details: {} };
          store.recordProviderTerminal(created.id, { owner, token: claim.claim_token }, failure.code === "deadline_exceeded" ? "timed_out" : "failed", { error: failure });
          store.transitionInteraction(fact.interaction_id, "rejected", failure.code);
          store.audit("router", "provider.observation_failed", "human_fact", fact.id, { failure });
        }
      }
      return observed;
    })();
    try {
      return await running;
    } finally {
      running = null;
    }
  }

  return {
    name: "provider-observations",
    tick,
    async start() {
      await tick();
      timer = setInterval(() => void tick(), Math.max(250, options.intervalMs ?? 2_000));
    },
    async stop() {
      if (timer) clearInterval(timer);
      timer = null;
      await running;
    },
  };
}

function jsonObject(raw: string): Record<string, unknown> {
  try {
    const value = JSON.parse(raw) as unknown;
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function contextForFact(store: Store, fact: FactRow): FactContext {
  let incidentId: string | null = null;
  let eventId: string | null = null;
  let sessionId: string | null = null;
  if (fact.target_type === "event") eventId = fact.target_id;
  else if (fact.target_type === "incident") incidentId = fact.target_id;
  else if (fact.target_type === "session") sessionId = fact.target_id;
  else if (fact.target_type === "escalation") {
    incidentId = (store.db.query("SELECT incident_id FROM escalations WHERE id = ?").get(fact.target_id) as { incident_id: string } | null)?.incident_id ?? null;
  } else if (fact.target_type === "decision") {
    incidentId = (store.db.query("SELECT incident_id FROM decisions WHERE id = ?").get(fact.target_id) as { incident_id: string } | null)?.incident_id ?? null;
  } else if (fact.target_type === "effect") {
    const effect = store.getEffect(fact.target_id);
    eventId = effect?.lineage_json ? String(jsonObject(effect.lineage_json).event_id ?? "") || null : null;
  }
  if (incidentId && !eventId) {
    const incident = store.db.query("SELECT opened_by_event, car_session_id FROM incidents WHERE id = ?").get(incidentId) as { opened_by_event: string; car_session_id: string | null } | null;
    eventId = incident?.opened_by_event ?? null;
    sessionId = incident?.car_session_id ?? sessionId;
  }
  const event = eventId ? store.db.query("SELECT source_vendor, source_host, car_session_id FROM events WHERE id = ?").get(eventId) as { source_vendor: string; source_host: string; car_session_id: string | null } | null : null;
  sessionId = event?.car_session_id ?? sessionId;
  const session = sessionId ? store.db.query("SELECT host, repo, repo_verified FROM sessions WHERE car_session_id = ?").get(sessionId) as { host: string; repo: string | null; repo_verified: number } | null : null;
  const host = session?.host ?? event?.source_host;
  const selection: ProviderSelectionContext = {
    ...(event?.source_vendor ? { source: event.source_vendor } : {}),
    ...(host ? { host } : {}),
    ...(session?.repo_verified === 1 && session.repo ? { repo: session.repo } : {}),
    ...(incidentId ? { incident_id: incidentId } : {}),
  };
  return { incidentId, selection };
}
