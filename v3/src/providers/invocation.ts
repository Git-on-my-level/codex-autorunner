import { payloadSha256 } from "../contract/ids.ts";
import { ProviderError, failureFromUnknown } from "./errors.ts";
import type {
  CapabilityProvider,
  ContextBundle,
  ContextQuery,
  EffectProposalPacket,
  HumanOrDecisionOutcome,
  IncidentPacket,
  PolicyAdvice,
  PolicyVerdict,
  ProviderDecision,
  ProviderEventEnvelope,
  ProviderFailure,
  ProviderHealth,
} from "./types.ts";
import { OperatorDecision, PolicyAdvice as PolicyAdviceSchema, ContextBundle as ContextBundleSchema } from "./types.ts";
import { makeProviderEvent, validateProviderEvents } from "./events.ts";

export type CapabilityResponse = ProviderDecision | PolicyAdvice | ContextBundle | void;

export interface ProviderCallResult<T extends CapabilityResponse> {
  value: T;
  events: ProviderEventEnvelope[];
}

/** Result shape used by Hermes/ACP adapters. */
export interface PublicProviderResponse<T> {
  result: T;
  events: unknown[];
}

const inflight = new Map<string, { fingerprint: string; promise: Promise<unknown> }>();

function methodFor(provider: CapabilityProvider, capability: string): ((input: never) => Promise<unknown>) | undefined {
  if (capability === "operator" && provider.decide) return (input) => provider.decide!(input as never);
  if (capability === "policy" && provider.evaluate) return (input) => provider.evaluate!(input as never);
  if (capability === "memory" && provider.context) return (input) => provider.context!(input as never);
  return undefined;
}

function deadlineMs(deadlineAt: string, descriptor: CapabilityProvider["descriptor"], requestId: string): number {
  const time = Date.parse(deadlineAt);
  if (!Number.isFinite(time)) throw new ProviderError("invalid_request", "deadline_at must be an ISO timestamp", descriptor, requestId, { retryable: false });
  const remaining = time - Date.now();
  if (remaining <= 0) throw new ProviderError("deadline_exceeded", "provider request deadline has passed", descriptor, requestId, { retryable: false });
  return remaining;
}

async function raceDeadline<T>(
  provider: CapabilityProvider,
  requestId: string,
  deadlineAt: string,
  action: () => Promise<T>,
): Promise<T> {
  const ms = deadlineMs(deadlineAt, provider.descriptor, requestId);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new ProviderError("deadline_exceeded", "provider request exceeded its deadline", provider.descriptor, requestId, { retryable: true })), ms);
  });
  try {
    return await Promise.race([action(), timeout]);
  } catch (error) {
    if (error instanceof ProviderError && error.failure.code === "deadline_exceeded") {
      const candidate = provider as CapabilityProvider & { cancel?: (requestId: string) => Promise<void> };
      await candidate.cancel?.(requestId).catch(() => undefined);
    }
    throw error;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

function requestFingerprint(input: unknown): string {
  return payloadSha256(input);
}

function checkResponse(
  capability: "operator" | "policy" | "memory",
  result: unknown,
  provider: CapabilityProvider,
  requestId: string,
): ProviderDecision | PolicyAdvice | ContextBundle {
  try {
    if (capability === "operator") return OperatorDecision.parse(result) as ProviderDecision;
    if (capability === "policy") return PolicyAdviceSchema.parse(result) as PolicyAdvice;
    return ContextBundleSchema.parse(result) as ContextBundle;
  } catch (cause) {
    throw new ProviderError("invalid_response", `provider returned an invalid ${capability} response`, provider.descriptor, requestId, {
      retryable: false,
      cause,
    });
  }
}

/**
 * Invoke a capability with request idempotency, deadlines, and strict typed
 * output validation. The map only deduplicates concurrent/replayed calls in a
 * process; the durable provider_invocations row remains the authority.
 */
export async function invokeCapability<T extends ProviderDecision | PolicyAdvice | ContextBundle>(
  provider: CapabilityProvider,
  capability: "operator" | "policy" | "memory",
  input: IncidentPacket | EffectProposalPacket | ContextQuery,
): Promise<ProviderCallResult<T>> {
  const method = methodFor(provider, capability);
  if (!method) throw new ProviderError("unsupported_capability", `${provider.descriptor.provider_instance} does not provide ${capability}`, provider.descriptor, input.request_id, { retryable: false });
  if (input.provider.provider_instance !== provider.descriptor.provider_instance || input.provider.config_fingerprint !== provider.descriptor.config_fingerprint) {
    throw new ProviderError("invalid_request", "request provider identity does not match the selected provider", provider.descriptor, input.request_id, { retryable: false });
  }
  const key = `${provider.descriptor.provider_instance}\u0000${capability}\u0000${input.request_id}`;
  const fingerprint = requestFingerprint(input);
  const existing = inflight.get(key);
  if (existing) {
    if (existing.fingerprint !== fingerprint) throw new ProviderError("invalid_request", "request id was reused with a different payload", provider.descriptor, input.request_id, { retryable: false, details: { request_id: input.request_id } });
    return (await existing.promise) as ProviderCallResult<T>;
  }

  const promise = (async (): Promise<ProviderCallResult<T>> => {
    try {
      const raw = await raceDeadline(provider, input.request_id, input.deadline_at, () => method(input as never));
      let result: unknown = raw;
      let events: ProviderEventEnvelope[];
      if (raw && typeof raw === "object" && "result" in raw && "events" in raw) {
        const response = raw as PublicProviderResponse<unknown>;
        const parsed = validateProviderEvents(response.events, provider.descriptor, input.request_id);
        events = parsed as ProviderEventEnvelope[];
        result = response.result;
      } else {
        // In-process providers still expose the same public event semantics.
        const now = new Date().toISOString();
        const invocationId = `inv_${input.request_id}`;
        events = [
          makeProviderEvent(provider.descriptor, { invocation_id: invocationId, request_id: input.request_id, type: "started", seq: 0, ts: now, payload: {} }),
          makeProviderEvent(provider.descriptor, { invocation_id: invocationId, request_id: input.request_id, type: "terminal_result", seq: 1, ts: now, payload: { outcome: "succeeded" } }),
        ] as ProviderEventEnvelope[];
      }
      const typed = checkResponse(capability, result, provider, input.request_id) as T;
      return { value: typed, events };
    } catch (error) {
      const failure = failureFromUnknown(error, provider.descriptor, input.request_id);
      throw Object.assign(new ProviderError(failure.code, failure.message, provider.descriptor, input.request_id, {
        retryable: failure.retryable,
        details: failure.details,
        cause: error,
      }), { failure });
    }
  })();
  inflight.set(key, { fingerprint, promise });
  try {
    return await promise;
  } finally {
    inflight.delete(key);
  }
}

export function providerFailureFrom(error: unknown, provider: CapabilityProvider, requestId: string): ProviderFailure {
  return failureFromUnknown(error, provider.descriptor, requestId);
}
