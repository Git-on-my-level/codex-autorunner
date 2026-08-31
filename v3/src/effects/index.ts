/**
 * Core effect execution. A provider submits an EffectProposal and may receive
 * a structured result, but never an executor or a direct side-effect handle.
 * The executor is deliberately a separate composition-root object so every
 * adapter invocation crosses the safety kernel and its lease fence.
 */
import {
  createSafetyKernel,
  type EffectProposal,
  type EffectRecord,
  type SafetyKernel,
  type SafetyVerdict,
  type TerminalOutcome,
} from "../safety/index.ts";

export interface EffectAdapterContext {
  effect: EffectRecord;
  /** Read-only metadata for adapters; no runner or executor is exposed. */
  readonly metadata: Readonly<Record<string, unknown>>;
}

export interface EffectExecutionResult {
  ok: boolean;
  output?: string;
  result?: unknown;
  /** Adapters may report uncertainty after a remote acknowledgement window. */
  outcome?: TerminalOutcome;
}

export interface CoreEffectAdapter {
  readonly type: EffectRecord["type"];
  execute(context: EffectAdapterContext): Promise<EffectExecutionResult> | EffectExecutionResult;
}

export interface EffectExecutionResponse {
  ok: boolean;
  state: EffectRecord["state"];
  outcome: TerminalOutcome | null;
  effect: EffectRecord;
  verdict: SafetyVerdict;
  output: string;
}

export interface EffectExecutorOptions {
  kernel?: SafetyKernel;
  adapters?: Iterable<CoreEffectAdapter>;
  owner?: string;
  metadata?: Record<string, unknown>;
  leaseMs?: number;
}

function outputOf(result: EffectExecutionResult): string {
  if (typeof result.output === "string") return result.output;
  if (result.result === undefined || result.result === null) return "";
  return typeof result.result === "string" ? result.result : JSON.stringify(result.result);
}

/** Map adapter outcomes into the closed lifecycle vocabulary. */
export function terminalOutcome(result: EffectExecutionResult): TerminalOutcome {
  if (result.outcome) return result.outcome;
  return result.ok ? "ok" : "failed";
}

export class EffectExecutor {
  readonly kernel: SafetyKernel;
  private readonly adapters = new Map<string, CoreEffectAdapter>();
  private readonly owner: string;
  private readonly metadata: Readonly<Record<string, unknown>>;
  private readonly leaseMs: number;

  constructor(options: EffectExecutorOptions = {}) {
    this.kernel = options.kernel ?? createSafetyKernel();
    this.owner = options.owner ?? "car-effect-worker";
    this.metadata = Object.freeze({ ...(options.metadata ?? {}) });
    this.leaseMs = options.leaseMs ?? 120_000;
    for (const adapter of options.adapters ?? []) this.register(adapter);
  }

  /** Registration is core composition; providers never call this at runtime. */
  register(adapter: CoreEffectAdapter): void {
    if (this.adapters.has(adapter.type)) throw new Error(`duplicate effect adapter '${adapter.type}'`);
    this.adapters.set(adapter.type, adapter);
  }

  async execute(proposal: EffectProposal, grantId?: string | null): Promise<EffectExecutionResponse> {
    const authorized = this.kernel.authorize(proposal, grantId);
    if (!authorized.verdict.allowed) {
      return {
        ok: false,
        state: authorized.effect.state,
        outcome: authorized.effect.terminal_outcome,
        effect: authorized.effect,
        verdict: authorized.verdict,
        output: `blocked (${authorized.verdict.code}): ${authorized.verdict.reason}`,
      };
    }

    const claim = this.kernel.claim(authorized.effect.intent_id, this.owner, this.leaseMs);
    if (!claim) {
      const current = this.kernel.ledger.getEffect(authorized.effect.intent_id) ?? authorized.effect;
      const verdict = current.state === "blocked"
        ? current.safety_verdict
        : { allowed: false as const, code: "lease_lost" as const, reason: "effect is already claimed or no longer pending", grant_id: current.grant_id };
      return {
        ok: false,
        state: current.state,
        outcome: current.terminal_outcome,
        effect: current,
        verdict,
        output: "effect is already claimed or no longer pending",
      };
    }

    const adapter = this.adapters.get(claim.effect.type);
    let result: EffectExecutionResult;
    let renewal: ReturnType<typeof setInterval> | null = null;
    let leaseLost = false;
    renewal = setInterval(() => {
      if (!this.kernel.renew(claim.effect.intent_id, this.owner, claim.token, this.leaseMs)) leaseLost = true;
    }, Math.max(10, Math.floor(this.leaseMs / 3)));
    try {
      if (!adapter) {
        result = { ok: false, output: `no core adapter registered for '${claim.effect.type}'` };
      } else {
        result = await adapter.execute({ effect: claim.effect, metadata: this.metadata });
      }
    } catch (error) {
      result = { ok: false, output: `adapter '${claim.effect.type}' failed: ${String(error)}` };
    } finally {
      if (renewal) clearInterval(renewal);
    }
    // Close the gap between the last timer renewal and the terminal CAS.
    if (!this.kernel.renew(claim.effect.intent_id, this.owner, claim.token, this.leaseMs)) {
      leaseLost = true;
    }
    if (leaseLost) throw new Error(`effect '${claim.effect.intent_id}' lost its execution lease; outcome requires reconciliation`);

    const outcome = terminalOutcome(result);
    // This is the sole terminal write. Optional notification, timeline, and
    // provider-observation work must happen after this call and can be replayed.
    const terminal = this.kernel.recordTerminal(claim.effect.intent_id, this.owner, claim.token, outcome, result.result ?? { output: outputOf(result) });
    return {
      ok: outcome === "ok",
      state: terminal.state,
      outcome: terminal.terminal_outcome,
      effect: terminal,
      verdict: terminal.safety_verdict,
      output: outputOf(result),
    };
  }
}

export function createEffectExecutor(options: EffectExecutorOptions = {}): EffectExecutor {
  return new EffectExecutor(options);
}

/** Small adapter constructor useful for core-owned reply/notify implementations. */
export function effectAdapter(
  type: EffectRecord["type"],
  execute: (context: EffectAdapterContext) => Promise<EffectExecutionResult> | EffectExecutionResult,
): CoreEffectAdapter {
  return { type, execute };
}
