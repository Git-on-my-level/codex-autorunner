import { ProviderError } from "./errors.ts";
import { validateProviderEvents } from "./events.ts";
import { payloadSha256 } from "../contract/ids.ts";
import {
  ContextBundle,
  OperatorDecision,
  PolicyAdvice,
  type CapabilityProvider,
  type ContextBundle as ContextBundleType,
  type ContextQuery,
  type EffectProposalPacket,
  type HumanOrDecisionOutcome,
  type IncidentPacket,
  type PolicyAdvice as PolicyAdviceType,
  type ProviderDecision,
  type ProviderDescriptor,
  type ProviderHealth,
  type ProviderPreflight,
} from "./types.ts";
import type { PublicProviderResponse } from "./invocation.ts";

export const HERMES_PROVIDER_VERSION = "hermes-acp-1.0.0";
export const ACP_PROTOCOL_VERSION = "1";

/** Public ACP lifecycle only. Implementations must not expose HERMES_HOME/files. */
export interface HermesAcpLifecycle {
  initialize(input: {
    provider_instance: string;
    profile: string;
    state_root: string;
    config_fingerprint: string;
  }): Promise<HermesAcpHandshake>;
  invoke(input: HermesPublicRequest, signal?: AbortSignal): Promise<PublicProviderResponse<unknown>>;
  cancel(requestId: string): Promise<void>;
  close?(): Promise<void>;
}

export interface HermesAcpHandshake {
  protocol_version: string;
  server_name?: string;
  server_version?: string;
  capabilities: string[];
}

export interface HermesPublicRequest {
  contract: "car.hermes-request.v1";
  request_id: string;
  capability: "operator" | "policy" | "memory";
  deadline_at: string;
  provider_instance: string;
  profile: string;
  continuity_key: string;
  payload: IncidentPacket | EffectProposalPacket | ContextQuery | HumanOrDecisionOutcome;
}

export interface HermesProviderOptions {
  descriptor: ProviderDescriptor;
  profile: string;
  lifecycle: HermesAcpLifecycle;
  timeoutGraceMs?: number;
}

function isoNow(): string {
  return new Date().toISOString();
}

function isPublicResponse(value: unknown): value is PublicProviderResponse<unknown> {
  return Boolean(value && typeof value === "object" && "result" in value && "events" in value && Array.isArray((value as { events?: unknown }).events));
}

/** Hermes adapter. It intentionally has no API for private transcript/session files. */
export class HermesProvider implements CapabilityProvider {
  readonly descriptor: ProviderDescriptor;
  readonly profile: string;
  private readonly lifecycle: HermesAcpLifecycle;
  private handshake: HermesAcpHandshake | null = null;
  private lastProgressAt: string | null = null;
  private readonly completed = new Map<string, { fingerprint: string; value: unknown }>();

  constructor(options: HermesProviderOptions) {
    this.descriptor = options.descriptor;
    this.profile = options.profile;
    this.lifecycle = options.lifecycle;
  }

  async preflight(): Promise<ProviderPreflight> {
    try {
      const handshake = await this.lifecycle.initialize({
        provider_instance: this.descriptor.provider_instance,
        profile: this.profile,
        state_root: this.descriptor.state_root,
        config_fingerprint: this.descriptor.config_fingerprint,
      });
      this.handshake = handshake;
      const ready = handshake.protocol_version === ACP_PROTOCOL_VERSION;
      return {
        ready,
        checked_at: isoNow(),
        diagnostics: ready
          ? [{ code: "hermes.acp.ready", message: `Hermes ACP ${handshake.protocol_version} is available`, severity: "info" }]
          : [{ code: "hermes.acp.version_mismatch", message: `expected ACP ${ACP_PROTOCOL_VERSION}, received ${handshake.protocol_version}`, severity: "error" }],
      };
    } catch (error) {
      return {
        ready: false,
        checked_at: isoNow(),
        diagnostics: [{ code: "hermes.acp.unavailable", message: error instanceof Error ? error.message : String(error), severity: "error" }],
      };
    }
  }

  async health(): Promise<ProviderHealth> {
    return {
      healthy: this.handshake?.protocol_version === ACP_PROTOCOL_VERSION,
      checked_at: isoNow(),
      semantic_progress_at: this.lastProgressAt,
      details: { protocol: this.handshake?.protocol_version ?? null, capabilities: this.handshake?.capabilities ?? [] },
    };
  }

  private async call(capability: "operator" | "policy" | "memory", input: HermesPublicRequest["payload"] & { request_id: string; deadline_at: string }): Promise<unknown> {
    if (!this.descriptor.capabilities.includes(capability)) {
      throw new ProviderError("unsupported_capability", `Hermes instance does not advertise ${capability}`, this.descriptor, input.request_id, { retryable: false });
    }
    if (this.handshake && this.handshake.protocol_version !== ACP_PROTOCOL_VERSION) {
      throw new ProviderError("version_mismatch", `Hermes ACP protocol ${this.handshake.protocol_version} is incompatible`, this.descriptor, input.request_id, { retryable: false });
    }
    if (!this.handshake) {
      throw new ProviderError("not_ready", "Hermes provider has not passed ACP preflight", this.descriptor, input.request_id, { retryable: true });
    }
    const deadline = Date.parse(input.deadline_at);
    if (!Number.isFinite(deadline)) throw new ProviderError("invalid_request", "deadline_at must be an ISO timestamp", this.descriptor, input.request_id, { retryable: false });
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new ProviderError("deadline_exceeded", "Hermes request deadline has passed", this.descriptor, input.request_id, { retryable: false });
    const completionKey = `${capability}\u0000${input.request_id}`;
    const fingerprint = payloadSha256({ capability, payload: input });
    const existing = this.completed.get(completionKey);
    if (existing !== undefined) {
      if (existing.fingerprint !== fingerprint) {
        throw new ProviderError("invalid_request", "Hermes request id was reused with a different payload", this.descriptor, input.request_id, { retryable: false });
      }
      return existing.value;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    const request: HermesPublicRequest = {
      contract: "car.hermes-request.v1",
      request_id: input.request_id,
      capability,
      deadline_at: input.deadline_at,
      provider_instance: this.descriptor.provider_instance,
      profile: this.profile,
      continuity_key: this.descriptor.continuity_key,
      payload: input,
    };
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
        void this.lifecycle.cancel(input.request_id).catch(() => undefined);
        reject(new ProviderError("deadline_exceeded", "Hermes ACP request exceeded its deadline", this.descriptor, input.request_id, { retryable: true }));
      }, remaining);
    });
    try {
      const raw = await Promise.race([this.lifecycle.invoke(request, controller.signal), timeout]);
      if (!isPublicResponse(raw)) throw new ProviderError("invalid_response", "Hermes ACP response is not a public result envelope", this.descriptor, input.request_id, { retryable: false });
      const events = validateProviderEvents(raw.events, this.descriptor, input.request_id);
      const terminal = events[events.length - 1]!;
      if (terminal.payload.outcome !== "succeeded") {
        const outcome = String(terminal.payload.outcome ?? "unknown");
        throw new ProviderError(outcome === "timed_out" ? "deadline_exceeded" : "transport_protocol", `Hermes ACP terminal outcome: ${outcome}`, this.descriptor, input.request_id, { retryable: outcome === "unknown" });
      }
      this.completed.set(completionKey, { fingerprint, value: raw.result });
      // A process-local replay cache is only an optimization; keep its size
      // bounded because durable provider_invocations rows remain the authority.
      if (this.completed.size > 1024) this.completed.delete(this.completed.keys().next().value!);
      this.lastProgressAt = isoNow();
      return raw.result;
    } catch (error) {
      if (timedOut) {
        throw new ProviderError("deadline_exceeded", "Hermes ACP request exceeded its deadline", this.descriptor, input.request_id, { retryable: true, cause: error });
      }
      if (error instanceof ProviderError) throw error;
      throw new ProviderError("transport_protocol", error instanceof Error ? error.message : String(error), this.descriptor, input.request_id, { retryable: true, cause: error });
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  }

  async decide(input: IncidentPacket): Promise<ProviderDecision> {
    const result = await this.call("operator", input);
    try {
      return OperatorDecision.parse(result) as ProviderDecision;
    } catch (cause) {
      throw new ProviderError("invalid_response", "Hermes operator response failed car.operator.v1 validation", this.descriptor, input.request_id, { retryable: false, cause });
    }
  }

  async evaluate(input: EffectProposalPacket): Promise<PolicyAdviceType> {
    const result = await this.call("policy", input);
    try {
      return PolicyAdvice.parse(result) as PolicyAdviceType;
    } catch (cause) {
      throw new ProviderError("invalid_response", "Hermes policy response failed car.policy.v1 validation", this.descriptor, input.request_id, { retryable: false, cause });
    }
  }

  async context(input: ContextQuery): Promise<ContextBundleType> {
    const result = await this.call("memory", input);
    try {
      return ContextBundle.parse(result) as ContextBundleType;
    } catch (cause) {
      throw new ProviderError("invalid_response", "Hermes memory response failed car.memory.v1 validation", this.descriptor, input.request_id, { retryable: false, cause });
    }
  }

  async observe(input: HumanOrDecisionOutcome): Promise<void> {
    await this.call("memory", input);
  }

  async cancel(requestId: string): Promise<void> {
    await this.lifecycle.cancel(requestId);
  }

  async close(): Promise<void> {
    await this.lifecycle.close?.();
  }
}

export function createHermesProvider(options: HermesProviderOptions): HermesProvider {
  return new HermesProvider(options);
}
