/**
 * The closed triage toolset (DESIGN §5). Nothing outside this table is callable:
 * an unknown tool name is a protocol violation and forces escalation.
 *
 * Every mutating tool is gated by the *executor*, not the prompt — the model can
 * ask for anything; policy decides whether it happens.
 */
import type { Store, EventRow } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import type { ActionBus, MemoryReader, MemoryWriter, PolicyPort } from "../ports.ts";
import type { PolicyEngine } from "../policy/index.ts";
import { answerPermission } from "../permission_park.ts";
import { actionDedupeHash } from "./dedupe.ts";
import type { TriageRepo } from "./repo.ts";

export interface ToolSpec {
  name: string;
  description: string;
  schema: Record<string, unknown>;
}

const str = (description: string) => ({ type: "string", description });
const obj = (description: string) => ({ type: "object", description, additionalProperties: true });

function schema(properties: Record<string, unknown>, required: string[] = []): Record<string, unknown> {
  return { type: "object", properties, required, additionalProperties: false };
}

/** Terminal tools end the run. Exactly one must be called. */
export const TERMINAL_TOOLS = new Set(["resolve", "keep_informed", "escalate", "defer"]);

export const TOOL_SPECS: ToolSpec[] = [
  {
    name: "memory_search",
    description: "Full-text search over CAR's memory (rules, notes, episodes). Read-only.",
    schema: schema({ query: str("search text"), scope: obj("optional scope filter, e.g. {repo, vendor}") }, ["query"]),
  },
  {
    name: "memory_get",
    description: "Fetch one memory by id. Read-only.",
    schema: schema({ id: str("memory id") }, ["id"]),
  },
  {
    name: "session_context",
    description: "Session card for the incident: vendor, host, repo, cwd, title, state, recent event counts.",
    schema: schema({}),
  },
  {
    name: "read_session_tail",
    description: "Bounded read of the most recent events on this session (newest last).",
    schema: schema({ limit: { type: "integer", description: "max events, capped at 20" } }),
  },
  {
    name: "run_probe",
    description: "Run a read-only allowlisted probe template with typed args. Never mutates.",
    schema: schema({ template_id: str("probe template id"), args: obj("typed template args") }, ["template_id"]),
  },
  {
    name: "reply_to_agent",
    description: "Send free text back to the blocked agent through its response channel.",
    schema: schema({ session_id: str("car session id (optional; defaults to the incident session)"), text: str("reply text") }, ["text"]),
  },
  {
    name: "approve_permission",
    description: "Approve a pending permission request on this incident.",
    schema: schema({ event_id: str("permission event id (optional; defaults to the newest one in the batch)"), reason: str("why") }),
  },
  {
    name: "deny_permission",
    description: "Deny a pending permission request on this incident.",
    schema: schema({ event_id: str("permission event id (optional; defaults to the newest one in the batch)"), reason: str("why") }),
  },
  {
    name: "run_action",
    description: "Run a mutating allowlisted action template. Subject to policy class enablement and rate limits.",
    schema: schema({ template_id: str("action template id"), args: obj("typed template args") }, ["template_id"]),
  },
  {
    name: "memory_propose",
    description: "Propose a memory. Always lands status=pending; never grants autonomy.",
    schema: schema(
      { kind: str("preference|autonomy|fact|summary"), content: obj("memory content"), scope: obj("scope selector") },
      ["kind", "content"],
    ),
  },
  {
    name: "resolve",
    description: "TERMINAL: the incident is handled; nothing needs David.",
    schema: schema({ summary: str("one-line summary of what happened") }, ["summary"]),
  },
  {
    name: "keep_informed",
    description: "TERMINAL: nothing to do; record for the digest without paging David.",
    schema: schema({ summary: str("one-line summary") }, ["summary"]),
  },
  {
    name: "escalate",
    description: "TERMINAL: David must decide. Use whenever in doubt.",
    schema: schema(
      {
        severity: { type: "string", enum: ["notice", "attention", "urgent"], description: "page urgency" },
        question: str("the exact question David must answer"),
        suggested_action: obj("optional suggested action {class, args, label}"),
      },
      ["question"],
    ),
  },
  {
    name: "defer",
    description: "TERMINAL: snooze this incident until a time.",
    schema: schema({ until: str("ISO-8601 timestamp"), reason: str("why deferring") }, ["until"]),
  },
];

const TOOL_NAMES = new Set(TOOL_SPECS.map((t) => t.name));
export function isKnownTool(name: string): boolean {
  return TOOL_NAMES.has(name);
}

/** Policy action class for each mutating tool. */
export function actionClassFor(tool: string, args: Record<string, unknown>): string | null {
  switch (tool) {
    case "run_probe":
      return "probe";
    case "reply_to_agent":
      return "reply";
    case "approve_permission":
    case "deny_permission":
      return "approve_permission";
    case "run_action":
      return `exec.${String(args.template_id ?? "unknown")}`;
    default:
      return null;
  }
}

export interface TerminalCall {
  tool: "resolve" | "keep_informed" | "escalate" | "defer";
  args: Record<string, unknown>;
}

export interface ToolContext {
  store: Store;
  config: CarConfig;
  repo: TriageRepo;
  policy: PolicyPort;
  actions: ActionBus;
  memoryReader: MemoryReader;
  memoryWriter: MemoryWriter;
  incidentId: string;
  decisionId: string;
  carSessionId: string | null;
  /** Events in the coalesced batch, oldest first. */
  events: EventRow[];
  /**
   * Set when running under an `autonomy=granted` memory rule. Class *enablement*
   * is then not required — DESIGN §5 keeps `approve_permission` globally
   * disabled precisely so it can only be granted per-rule via memory promotion.
   * An explicit `forbid` verdict and every gate still apply.
   */
  granted?: boolean;
}

export interface ToolResult {
  ok: boolean;
  /** JSON-serialisable payload fed back to the model as the tool result. */
  output: unknown;
}

function gate(ctx: ToolContext, actionClass: string, args: Record<string, unknown>): { hash: string; blocked: string | null } {
  const hash = actionDedupeHash(actionClass, args);
  if (ctx.policy.escalateOnly()) {
    return { hash, blocked: "escalate-only mode is active; autonomous actions are disabled" };
  }
  const verdict = ctx.policy.check(actionClass, args);
  if (verdict === "forbid" || (verdict !== "auto" && !ctx.granted)) {
    return { hash, blocked: `policy verdict ${verdict} for class ${actionClass}` };
  }
  const gateFn = ctx.policy.gate as PolicyEngine["gate"];
  const reason = gateFn.call(ctx.policy, actionClass, hash, ctx.carSessionId);
  return { hash, blocked: reason };
}

/** Record the attempt (blocked or not) so the safety rails have evidence. */
function recordAttempt(
  ctx: ToolContext,
  actionClass: string,
  args: Record<string, unknown>,
  hash: string,
  blocked: string | null,
  outcome?: { ok: boolean; result: unknown },
): void {
  ctx.repo.recordAction({
    decisionId: ctx.decisionId,
    actionClass,
    args,
    policyVerdict: blocked ? "blocked" : "auto",
    dedupeHash: hash,
    state: blocked ? "failed" : outcome?.ok ? "ok" : "failed",
    result: blocked ? { blocked } : outcome?.result,
  });
}

function pickPermissionEvent(ctx: ToolContext, explicit: unknown): EventRow | null {
  if (typeof explicit === "string" && explicit) {
    return ctx.events.find((e) => e.id === explicit) ?? null;
  }
  const candidates = ctx.events.filter((e) => e.type === "attention.permission");
  return candidates[candidates.length - 1] ?? ctx.events[ctx.events.length - 1] ?? null;
}

function responseChannel(row: EventRow | null) {
  if (!row?.response_channel_json) return null;
  try {
    return JSON.parse(row.response_channel_json) as { kind: string; hint?: Record<string, unknown> };
  } catch {
    return null;
  }
}

/**
 * Execute one non-terminal tool call. Terminal tools are handled by the loop.
 * Never throws: a failed tool is a result the model can react to.
 */
export async function executeTool(
  ctx: ToolContext,
  tool: string,
  rawArgs: Record<string, unknown>,
): Promise<ToolResult> {
  const args = rawArgs ?? {};
  try {
    switch (tool) {
      case "memory_search": {
        const hits = ctx.memoryReader.search(
          String(args.query ?? ""),
          (args.scope as Record<string, string>) ?? undefined,
        );
        return { ok: true, output: { hits } };
      }
      case "memory_get": {
        const hit = ctx.memoryReader.get(String(args.id ?? ""));
        return { ok: hit !== null, output: hit ?? { error: "not found" } };
      }
      case "session_context": {
        if (!ctx.carSessionId) return { ok: true, output: { session: null, note: "sessionless event" } };
        const session = ctx.store.db
          .query("SELECT * FROM sessions WHERE car_session_id = ?")
          .get(ctx.carSessionId);
        const refs = ctx.store.db
          .query("SELECT vendor, host, native_id FROM session_refs WHERE car_session_id = ?")
          .all(ctx.carSessionId);
        return { ok: true, output: { session, refs } };
      }
      case "read_session_tail": {
        const limit = Math.min(Math.max(Number(args.limit ?? 10) || 10, 1), 20);
        if (!ctx.carSessionId) return { ok: true, output: { events: [] } };
        const rows = ctx.store.db
          .query(
            `SELECT id, ts, type, severity, title, substr(body, 1, 800) body FROM events
              WHERE car_session_id = ? ORDER BY received_at DESC LIMIT ?`,
          )
          .all(ctx.carSessionId, limit) as unknown[];
        return { ok: true, output: { events: rows.reverse() } };
      }
      case "run_probe": {
        const templateId = String(args.template_id ?? "");
        const targs = (args.args as Record<string, unknown>) ?? {};
        const { hash, blocked } = gate(ctx, "probe", { template_id: templateId, ...targs });
        if (blocked) {
          recordAttempt(ctx, "probe", { template_id: templateId, ...targs }, hash, blocked);
          return { ok: false, output: { blocked } };
        }
        const res = await ctx.actions.runTemplate(templateId, targs, {
          decisionId: ctx.decisionId,
          mutating: false,
          recordedByCaller: true, // recordAttempt below owns the actions row
        });
        recordAttempt(ctx, "probe", { template_id: templateId, ...targs }, hash, null, {
          ok: res.ok,
          result: res,
        });
        return { ok: res.ok, output: res };
      }
      case "run_action": {
        const templateId = String(args.template_id ?? "");
        const targs = (args.args as Record<string, unknown>) ?? {};
        const actionClass = `exec.${templateId}`;
        const { hash, blocked } = gate(ctx, actionClass, targs);
        if (blocked) {
          recordAttempt(ctx, actionClass, targs, hash, blocked);
          return { ok: false, output: { blocked } };
        }
        const res = await ctx.actions.runTemplate(templateId, targs, {
          decisionId: ctx.decisionId,
          mutating: true,
          recordedByCaller: true, // recordAttempt below owns the actions row
        });
        recordAttempt(ctx, actionClass, targs, hash, null, { ok: res.ok, result: res });
        return { ok: res.ok, output: res };
      }
      case "reply_to_agent": {
        const sessionId = (typeof args.session_id === "string" && args.session_id) || ctx.carSessionId;
        const text = String(args.text ?? "");
        if (!sessionId) return { ok: false, output: { error: "no session to reply to" } };
        const { hash, blocked } = gate(ctx, "reply", { session_id: sessionId, text });
        if (blocked) {
          recordAttempt(ctx, "reply", { session_id: sessionId, text }, hash, blocked);
          return { ok: false, output: { blocked } };
        }
        const channel = responseChannel(ctx.events[ctx.events.length - 1] ?? null);
        const result = await ctx.actions.deliver(sessionId, channel as never, { text });
        const ok = result === "delivered" || result === "degraded" || result === "queued";
        recordAttempt(ctx, "reply", { session_id: sessionId, text }, hash, null, { ok, result });
        return { ok, output: { delivery: result } };
      }
      case "approve_permission":
      case "deny_permission": {
        const approve = tool === "approve_permission";
        const target = pickPermissionEvent(ctx, args.event_id);
        if (!target) return { ok: false, output: { error: "no permission event in this batch" } };
        const gateArgs = { event_id: target.id, decision: approve ? "allow" : "deny" };
        /*
         * Content rail, checked before the class gates and only for approvals:
         * a grant is scoped to a repo or a request lineage, but what makes a
         * request dangerous is inside its text. Denying is always safe, so this
         * never stands between CAR and a "no".
         */
        if (approve) {
          const forbidden = ctx.policy.autoApprovalBlock(`${target.title}\n${target.body}`);
          if (forbidden) {
            const reason = `never auto-approved: ${forbidden}`;
            recordAttempt(ctx, "approve_permission", gateArgs, actionDedupeHash("approve_permission", gateArgs), reason);
            ctx.store.audit("daemon", "permission.auto_approval_blocked", "event", target.id, {
              matched: forbidden,
              incident_id: ctx.incidentId,
            });
            return { ok: false, output: { blocked: reason } };
          }
        }
        const { hash, blocked } = gate(ctx, "approve_permission", gateArgs);
        if (blocked) {
          recordAttempt(ctx, "approve_permission", gateArgs, hash, blocked);
          return { ok: false, output: { blocked } };
        }
        // Race-free path: answer the parked hook response in-band.
        const parkedAnswered = answerPermission(target.id, {
          decision: approve ? "allow" : "deny",
          reason: typeof args.reason === "string" ? args.reason : undefined,
        });
        // Fallback path: the park may have already timed out (or this is a
        // restarted daemon) — reply-back adapters still carry the answer.
        let delivery: string = parkedAnswered ? "parked" : "none";
        if (ctx.carSessionId) {
          delivery = await ctx.actions.deliver(ctx.carSessionId, responseChannel(target) as never, {
            approval: approve,
          });
        }
        const ok = parkedAnswered || delivery === "delivered" || delivery === "degraded" || delivery === "queued";
        recordAttempt(ctx, "approve_permission", gateArgs, hash, null, {
          ok,
          result: { parkedAnswered, delivery },
        });
        return { ok, output: { parked: parkedAnswered, delivery } };
      }
      case "memory_propose": {
        const id = ctx.memoryWriter.propose(
          String(args.kind ?? "fact"),
          (args.content as Record<string, unknown>) ?? {},
          (args.scope as Record<string, unknown>) ?? {},
        );
        return { ok: true, output: { memory_id: id, status: "pending" } };
      }
      default:
        return { ok: false, output: { error: `unknown tool ${tool}` } };
    }
  } catch (err) {
    ctx.store.audit("triage", "tool.error", "incident", ctx.incidentId, { tool, error: String(err) });
    return { ok: false, output: { error: String(err) } };
  }
}
