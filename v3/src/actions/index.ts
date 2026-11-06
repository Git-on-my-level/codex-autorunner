/**
 * WS-E — src/actions/: the ActionBus.
 *
 * Two jobs, both of which must never lose work quietly:
 *
 * 1. `deliver()` — reply-back (DESIGN §8). One adapter registry keyed by
 *    `response_channel.kind`, each kind resolving to a CHAIN of rungs that ends
 *    at the universal file inbox. Every attempt is audited; a reply that cannot
 *    reach its agent by any rung returns 'failed' and writes
 *    `reply.delivery_failed` so triage and the digest surface it. Non-negotiable
 *    #6: a dropped reply is the one unforgivable sin.
 *
 * 2. `runTemplate()` — the policy-enforcing executor (DESIGN §5). Templates are
 *    argv arrays with typed placeholders; args are validated, shell
 *    metacharacters are refused, the policy port decides, and every state
 *    transition lands in `actions` + `audit`.
 *
 * Everything that touches a subprocess goes through the injectable `Runner`
 * seam, and Multica HTTP through the injectable `FetchLike` seam, so the whole
 * module is testable without a network or a real vendor CLI.
 */
import { statSync } from "node:fs";
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import type { ActionBus, DeliveryResult, PolicyPort, ReplyPayload } from "../ports.ts";
import type { ResponseChannel, ResponseChannelKind } from "../contract/events.ts";
import { actionId, intentId } from "../contract/ids.ts";
import type { EffectProposal, SafetyKernel } from "../safety/index.ts";
import { createBunRunner, truncateOutput, type Runner } from "./runner.ts";
import { probeCapabilities, type CliCapabilities } from "./capabilities.ts";
import {
  buildArgv,
  dedupeHash,
  loadTemplates,
  policyClassFor,
  templatesPath,
  TemplateError,
  validateArgs,
  type LoadedTemplates,
  type TemplateSpec,
} from "./templates.ts";
import {
  defaultFetch,
  type Adapter,
  type AdapterOutcome,
  type DeliveryContext,
  type EnvLike,
  type FetchLike,
} from "./adapters/types.ts";
import { fileAdapter } from "./adapters/file.ts";
import { claudeHookHttpAdapter, claudeResumeAdapter } from "./adapters/claude.ts";
import { codexExecResumeAdapter } from "./adapters/codex.ts";
import { agentctlRunAdapter } from "./adapters/agentctl.ts";
import { multicaApiAdapter } from "./adapters/multica.ts";

export { createBunRunner, type RunResult, type Runner } from "./runner.ts";
export { probeCapabilities, supports, type CliCapabilities } from "./capabilities.ts";
export { DEFAULT_TEMPLATES_TOML, dedupeHash, templatesPath } from "./templates.ts";
export type { FetchLike } from "./adapters/types.ts";

/** Max bytes of subprocess output kept in an `actions.result_json` row. */
export const MAX_RESULT_BYTES = 8 * 1024;

export interface ActionBusDeps {
  /** Subprocess seam. Tests inject a fake; production uses Bun.spawn. */
  runner?: Runner;
  /** HTTP seam for multica-api. */
  httpFetch?: FetchLike;
  /** Environment lookup (CAR_MULTICA_URL / CAR_MULTICA_TOKEN). */
  env?: EnvLike;
  /** Optional v3 safety kernel. Omitted only for the v2-compatible scaffold path. */
  safety?: SafetyKernel;
}

export interface ActionRunOptions {
  decisionId: string;
  mutating: boolean;
  recordedByCaller?: boolean;
  grantId?: string | null;
  intentId?: string;
  scope?: EffectProposal["scope"];
  lineage?: EffectProposal["lineage"];
  deadlineAt?: string | null;
  costUsd?: number;
}

export interface CarActionBus extends ActionBus {
  /** v3 callers may carry the core grant and immutable request identity. */
  runTemplate(templateId: string, args: Record<string, unknown>, opts: ActionRunOptions): Promise<{ ok: boolean; output: string }>;
  /** Probe (or read the 24h-cached probe of) a vendor CLI. */
  probeCapabilities(vendor: string, opts?: { force?: boolean }): Promise<CliCapabilities>;
  /** Ids of the currently loaded templates (file or shipped defaults). */
  listTemplates(): string[];
}

/**
 * Adapter registry. Each ResponseChannelKind maps to the ordered rungs tried for
 * it; every chain ends at `fileAdapter`, the universal fallback.
 */
export const DELIVERY_CHAINS: Record<ResponseChannelKind, Adapter[]> = {
  "claude-hook-http": [claudeHookHttpAdapter, claudeResumeAdapter, fileAdapter],
  "claude-resume": [claudeResumeAdapter, fileAdapter],
  "codex-exec-resume": [codexExecResumeAdapter, fileAdapter],
  "agentctl-run": [agentctlRunAdapter, fileAdapter],
  "multica-api": [multicaApiAdapter, fileAdapter],
  file: [fileAdapter],
};

/** Plain-text rendering of a reply payload, as the agent will see it. */
export function renderPayloadText(payload: ReplyPayload): string {
  const parts: string[] = [];
  if (payload.approval !== undefined) parts.push(payload.approval ? "APPROVED" : "DENIED");
  const text = payload.text?.trim();
  if (text) parts.push(text);
  return parts.join("\n\n");
}

export function createActionBus(
  store: Store,
  config: CarConfig,
  policy: PolicyPort,
  deps: ActionBusDeps = {},
): CarActionBus {
  const runner = deps.runner ?? createBunRunner();
  const httpFetch = deps.httpFetch ?? defaultFetch();
  const env = deps.env ?? (process.env as EnvLike);

  /* ------------------------------------------------------ template registry */

  let cached: LoadedTemplates | null = null;

  function currentTemplates(): LoadedTemplates {
    let mtimeMs = 0;
    try {
      mtimeMs = statSync(templatesPath(config)).mtimeMs;
    } catch {
      mtimeMs = 0;
    }
    if (cached && cached.mtimeMs === mtimeMs) return cached;
    cached = loadTemplates(config);
    return cached;
  }

  /* -------------------------------------------------------------- deliver */

  async function attempt(adapter: Adapter, ctx: DeliveryContext): Promise<AdapterOutcome> {
    try {
      return await adapter.deliver(ctx);
    } catch (err) {
      return { status: "failed", reason: `${adapter.kind} threw: ${String(err)}` };
    }
  }

  async function deliver(
    carSessionId: string,
    channel: ResponseChannel | null,
    payload: ReplyPayload,
  ): Promise<DeliveryResult> {
    const kind: ResponseChannelKind = channel?.kind ?? "file";
    const chain = DELIVERY_CHAINS[kind] ?? DELIVERY_CHAINS.file;
    const ctx: DeliveryContext = {
      store,
      config,
      runner,
      httpFetch,
      env,
      carSessionId,
      channel,
      payload,
      text: renderPayloadText(payload),
      hint: (channel?.hint ?? {}) as Record<string, unknown>,
    };

    const attempts: { adapter: string; status: string; reason?: string }[] = [];

    for (let rung = 0; rung < chain.length; rung++) {
      const adapter = chain[rung]!;
      const outcome = await attempt(adapter, ctx);
      const reason = outcome.status === "delivered" ? undefined : outcome.reason;
      attempts.push({ adapter: adapter.kind, status: outcome.status, ...(reason ? { reason } : {}) });
      store.audit("adapter:bus", "reply.attempt", "session", carSessionId, {
        channel_kind: kind,
        adapter: adapter.kind,
        rung,
        outcome: outcome.status,
        reason: reason ?? null,
        detail: outcome.detail ?? {},
      });
      if (outcome.status !== "delivered") continue;

      // Landing on the file inbox means the reply is STAGED, not delivered. Say so.
      const stagedOnly = adapter.kind === "file";
      const result: DeliveryResult = !stagedOnly ? "delivered" : kind === "file" ? "queued" : "degraded";
      store.audit(
        "adapter:bus",
        result === "delivered" ? "reply.delivered" : result === "queued" ? "reply.queued" : "reply.degraded",
        "session",
        carSessionId,
        { channel_kind: kind, adapter: adapter.kind, rung, attempts, detail: outcome.detail ?? {} },
      );
      return result;
    }

    // Every rung, including the file inbox, failed. Loudly.
    store.audit("adapter:bus", "reply.delivery_failed", "session", carSessionId, {
      channel_kind: kind,
      attempts,
      payload: { approval: payload.approval ?? null, has_text: Boolean(payload.text) },
    });
    return "failed";
  }

  /* ---------------------------------------------------------- runTemplate */

  function recordAction(row: {
    class: string;
    decisionId: string;
    args: Record<string, unknown>;
    verdict: string;
    hash: string;
    state: string;
    startedAt?: string | null;
    finishedAt?: string | null;
    result?: unknown;
  }): string {
    const id = actionId();
    store.db
      .query(
        `INSERT INTO actions (id, decision_id, class, args_json, policy_verdict, dedupe_hash, state,
           started_at, finished_at, result_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        id,
        row.decisionId,
        row.class,
        JSON.stringify(row.args),
        row.verdict,
        row.hash,
        row.state,
        row.startedAt ?? null,
        row.finishedAt ?? null,
        row.result === undefined ? null : JSON.stringify(row.result),
      );
    return id;
  }

  function refuse(input: {
    templateId: string;
    decisionId: string;
    args: Record<string, unknown>;
    actionClass: string;
    verdict: string;
    reason: string;
    recordedByCaller?: boolean;
  }): { ok: false; output: string } {
    const now = store.clock.now().toISOString();
    const hash = dedupeHash(input.templateId, input.args);
    const id = input.recordedByCaller
      ? actionId()
      : recordAction({
          class: input.actionClass,
          decisionId: input.decisionId,
          args: input.args,
          verdict: input.verdict,
          hash,
          state: "failed",
          finishedAt: now,
          result: { refused: true, reason: input.reason },
        });
    store.audit("adapter:exec", "action.refused", "action", id, {
      template_id: input.templateId,
      decision_id: input.decisionId,
      class: input.actionClass,
      verdict: input.verdict,
      reason: input.reason,
      dedupe_hash: hash,
    });
    return { ok: false, output: `refused (${input.verdict}): ${input.reason}` };
  }

  async function runTemplate(
    templateId: string,
    args: Record<string, unknown>,
    opts: ActionRunOptions,
  ): Promise<{ ok: boolean; output: string }> {
    const base = {
      templateId,
      decisionId: opts.decisionId,
      args,
      ...(opts.recordedByCaller ? { recordedByCaller: true } : {}),
    };

    let registry: LoadedTemplates;
    try {
      registry = currentTemplates();
    } catch (err) {
      return refuse({
        ...base,
        actionClass: `template.${templateId}`,
        verdict: "forbid",
        reason: `templates.toml is invalid: ${String(err)}`,
      });
    }

    const spec: TemplateSpec | undefined = registry.templates[templateId];
    if (!spec) {
      return refuse({
        ...base,
        actionClass: `template.${templateId}`,
        verdict: "forbid",
        reason: `unknown template '${templateId}'`,
      });
    }

    const actionClass = policyClassFor(templateId, spec);

    // A read-only caller (the `run_probe` tool) may never reach a mutating template.
    if (spec.mutating && !opts.mutating) {
      return refuse({
        ...base,
        actionClass,
        verdict: "forbid",
        reason: `template '${templateId}' is mutating; not reachable from the read-only path`,
      });
    }

    let argv: string[];
    let cwd: string | undefined;
    try {
      const resolved = validateArgs(templateId, spec, args);
      const built = buildArgv(templateId, spec, resolved);
      argv = built.argv;
      cwd = built.cwd;
    } catch (err) {
      const reason = err instanceof TemplateError ? `${err.reason}: ${err.message}` : String(err);
      return refuse({ ...base, actionClass, verdict: "forbid", reason });
    }

    // Escalate-only mode (circuit breaker / budget stop / /panic) stops CAR from
    // changing the world; read-only probes stay available for context.
    if (spec.mutating && policy.escalateOnly()) {
      return refuse({
        ...base,
        actionClass,
        verdict: "escalate",
        reason: "escalate-only mode: mutating actions are suspended",
      });
    }

    const verdict = policy.check(actionClass, args);
    if (verdict !== "auto") {
      return refuse({
        ...base,
        actionClass,
        verdict,
        reason: `policy verdict '${verdict}' for class '${actionClass}'`,
      });
    }

    const hash = dedupeHash(templateId, args);
    const blocked = policy.gate(actionClass, hash);
    if (blocked) {
      return refuse({
        ...base,
        actionClass,
        verdict: /^blocked_/.test(blocked) ? blocked : "blocked_gate",
        reason: blocked,
      });
    }

    // The safety kernel is the only v3 authorization boundary. Keep the old
    // policy-only path above as a compatibility seam until the composition
    // root has migrated every caller; when supplied, no template can bypass
    // grants, content rails, deadlines, or panic/budget gates.
    const safety = deps.safety;
    const effect = safety
      ? safety.authorize(
          {
            intent_id: opts.intentId ?? intentId("effect"),
            type: "run_template",
            args: { template_id: templateId, ...args },
            scope: opts.scope ?? {},
            lineage: opts.lineage ?? { source_id: "car-action-bus", request_id: opts.decisionId },
            provider_policy_verdict: verdict,
            deadline_at: opts.deadlineAt ?? null,
            action_class: actionClass,
            cost_usd: opts.costUsd,
          },
          opts.grantId,
        )
      : null;
    if (effect && !effect.verdict.allowed) {
      return refuse({
        ...base,
        actionClass,
        verdict: `safety_${effect.verdict.code}`,
        reason: effect.verdict.reason,
      });
    }
    if (effect?.effect.state === "terminal_recorded") {
      const terminal = effect.effect.terminal_outcome;
      return { ok: terminal === "ok", output: terminal ? `replayed terminal outcome: ${terminal}` : "replayed terminal effect" };
    }
    // Fence the effect before touching the external world. A claim made after
    // the runner returns would leave a window where a restart/replay worker
    // could execute the same intent concurrently.
    const effectClaim = effect ? safety!.claim(effect.effect.intent_id, "car-action-bus") : null;
    if (effect && !effectClaim) {
      return refuse({
        ...base,
        actionClass,
        verdict: "safety_lease_lost",
        reason: "effect could not be exclusively claimed",
      });
    }

    /* -------------------------------------------------------------- execute */

    const startedAt = store.clock.now().toISOString();
    const id = opts.recordedByCaller
      ? actionId()
      : recordAction({
          class: actionClass,
          decisionId: opts.decisionId,
          args,
          verdict: "auto",
          hash,
          state: "pending",
        });
    if (!opts.recordedByCaller) {
      store.db.query("UPDATE actions SET state = 'running', started_at = ? WHERE id = ?").run(startedAt, id);
    }
    store.audit("adapter:exec", "action.started", "action", id, {
      template_id: templateId,
      decision_id: opts.decisionId,
      class: actionClass,
      argv,
      cwd: cwd ?? null,
      mutating: spec.mutating,
      dedupe_hash: hash,
    });

    let code: number;
    let stdout = "";
    let stderr = "";
    try {
      const res = await runner(argv, {
        ...(cwd ? { cwd } : {}),
        timeoutMs: spec.timeout_ms,
      });
      code = res.code;
      stdout = truncateOutput(res.stdout, MAX_RESULT_BYTES);
      stderr = truncateOutput(res.stderr, MAX_RESULT_BYTES);
    } catch (err) {
      code = -1;
      stderr = `runner threw: ${String(err)}`;
    }

    const finishedAt = store.clock.now().toISOString();
    const ok = code === 0;
    const result = { code, stdout, stderr };
    if (safety && effect && effectClaim) {
      safety.recordTerminal(effectClaim.effect.intent_id, "car-action-bus", effectClaim.token, ok ? "ok" : "failed", result);
    }
    if (!opts.recordedByCaller) {
      store.db
        .query("UPDATE actions SET state = ?, finished_at = ?, result_json = ? WHERE id = ?")
        .run(ok ? "ok" : "failed", finishedAt, JSON.stringify(result), id);
    }
    store.audit("adapter:exec", ok ? "action.ok" : "action.failed", "action", id, {
      template_id: templateId,
      decision_id: opts.decisionId,
      class: actionClass,
      code,
      dedupe_hash: hash,
    });

    return { ok, output: ok ? stdout : `exit ${code}\n${stderr || stdout}`.trim() };
  }

  return {
    deliver,
    runTemplate,
    probeCapabilities: (vendor, probeOpts) => probeCapabilities(store, runner, vendor, probeOpts ?? {}),
    listTemplates: () => Object.keys(currentTemplates().templates),
  };
}
