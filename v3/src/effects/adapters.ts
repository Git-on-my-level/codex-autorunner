/** Core-owned adapters for authorized provider effects. */
import { ResponseChannel, type ResponseChannel as ResponseChannelValue } from "../contract/events.ts";
import type { ChannelPort } from "../ports.ts";
import type { Store } from "../store/db.ts";
import type { CarActionBus } from "../actions/index.ts";
import { effectAdapter, type CoreEffectAdapter } from "./index.ts";

interface EffectSource {
  car_session_id: string | null;
  response_channel_json: string | null;
  incident_id: string | null;
}

function stringArg(args: Record<string, unknown>, key: string): string | null {
  const value = args[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function sourceFor(store: Store, eventId?: string): EffectSource | null {
  if (!eventId) return null;
  return (store.db.query(
    `SELECT e.car_session_id, e.response_channel_json,
            COALESCE(e.incident_id, i.id) AS incident_id
       FROM events e
       LEFT JOIN incidents i ON i.opened_by_event = e.id
      WHERE e.id = ?
      ORDER BY i.opened_at DESC LIMIT 1`,
  ).get(eventId) as EffectSource | null) ?? null;
}

function responseChannel(raw: string | null): ResponseChannelValue | null {
  if (!raw) return null;
  try {
    return ResponseChannel.parse(JSON.parse(raw));
  } catch {
    return null;
  }
}

function decisionFor(store: Store, requestId: string, incidentId: string | null): string | null {
  if (!incidentId) return null;
  const row = store.db.query(
    `SELECT d.id
       FROM decisions d
       JOIN provider_invocations p ON p.incident_id = d.incident_id
      WHERE p.request_id = ? AND d.incident_id = ?
      ORDER BY d.created_at DESC LIMIT 1`,
  ).get(requestId, incidentId) as { id: string } | null;
  return row?.id ?? null;
}

export function createCoreEffectAdapters(store: Store, actions: CarActionBus, channel: ChannelPort): CoreEffectAdapter[] {
  const delivery = (type: "reply" | "approve" | "deny") => effectAdapter(type, async ({ effect }) => {
    const source = sourceFor(store, effect.lineage.event_id);
    if (!source?.car_session_id) return { ok: false, output: `${type} requires a durable session lineage` };
    const text = stringArg(effect.args, "text") ?? undefined;
    const result = await actions.deliver(
      source.car_session_id,
      responseChannel(source.response_channel_json),
      type === "reply" ? { text } : { approval: type === "approve", ...(text ? { text } : {}) },
    );
    if (effect.lineage.event_id) {
      store.db.query("UPDATE events SET obligation_state = ? WHERE id = ? AND obligation_state NOT IN ('resolved','cancelled','expired')")
        .run(result === "delivered" ? "delivered" : result === "failed" ? "open" : "staged", effect.lineage.event_id);
      store.audit("effects", "reply.delivery_observed", "event", effect.lineage.event_id, { delivery: result });
    }
    return {
      ok: result === "delivered",
      // Fallback acceptance is not completed delivery. This coarse effect
      // outcome stays uncertain; the result preserves queued vs degraded.
      outcome: result === "delivered" ? "ok" : result === "failed" ? "failed" : "uncertain",
      output: result,
      result: { delivery: result },
    };
  });

  return [
    effectAdapter("notify", ({ effect }) => {
      const text = stringArg(effect.args, "text");
      if (!text) return { ok: false, output: "notify requires non-empty text" };
      const source = sourceFor(store, effect.lineage.event_id);
      channel.sendNotify(text, source?.car_session_id ?? undefined);
      return { ok: true, output: "notification enqueued" };
    }),
    delivery("reply"),
    delivery("approve"),
    delivery("deny"),
    effectAdapter("probe", async ({ effect }) => {
      const vendor = stringArg(effect.args, "vendor");
      if (!vendor) return { ok: false, output: "probe requires vendor" };
      const result = await actions.probeCapabilities(vendor, { force: effect.args.force === true });
      return { ok: true, output: JSON.stringify(result), result };
    }),
    effectAdapter("run_template", async ({ effect }) => {
      const templateId = stringArg(effect.args, "template_id");
      if (!templateId) return { ok: false, output: "run_template requires template_id" };
      const source = sourceFor(store, effect.lineage.event_id);
      const decisionId = decisionFor(store, effect.lineage.request_id, source?.incident_id ?? null);
      if (!decisionId) return { ok: false, output: "run_template requires a durable provider decision" };
      const args = { ...effect.args };
      delete args.template_id;
      return actions.runTemplate(templateId, args, {
        decisionId,
        // Template metadata remains authoritative: true merely permits either
        // a read-only or mutating allowlisted template to reach its executor.
        mutating: true,
      });
    }),
  ];
}
