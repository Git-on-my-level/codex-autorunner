import { ProviderEvent, type ProviderEvent as ProviderEventType } from "../contract/lifecycle.ts";
import type { ProviderDescriptor } from "./types.ts";
import { ProviderError } from "./errors.ts";

export type ProviderEventInput = Omit<ProviderEventType, "contract" | "provider_id" | "provider_instance"> & {
  provider_id?: string;
  provider_instance?: string;
};

/** Build a wire-valid provider event with the common provenance filled in. */
export function makeProviderEvent(
  descriptor: ProviderDescriptor,
  input: Omit<ProviderEventInput, "provider_id" | "provider_instance">,
): ProviderEventType {
  return ProviderEvent.parse({
    contract: "car.provider-event.v1",
    provider_id: descriptor.provider_id,
    provider_instance: descriptor.provider_instance,
    ...input,
  });
}

/**
 * Validate the event stream's semantic envelope.  CAR accepts only public
 * protocol events as terminal evidence: a private transcript or a plain
 * response object cannot establish completion.
 */
export function validateProviderEvents(
  events: readonly unknown[],
  descriptor: ProviderDescriptor,
  requestId: string,
): ProviderEventType[] {
  let parsed: ProviderEventType[];
  try {
    parsed = events.map((event) => ProviderEvent.strict().parse(event));
  } catch (cause) {
    throw new ProviderError(
      "invalid_response",
      "provider emitted an invalid event",
      descriptor,
      requestId,
      { retryable: false, cause },
    );
  }
  if (parsed.length === 0) {
    throw new ProviderError(
      "invalid_response",
      "provider response has no public protocol events",
      descriptor,
      requestId,
      { retryable: false },
    );
  }
  for (const [index, event] of parsed.entries()) {
    if (event.request_id !== requestId) {
      throw new ProviderError(
        "invalid_response",
        "provider event request id does not match invocation",
        descriptor,
        requestId,
        { retryable: false, details: { event_request_id: event.request_id } },
      );
    }
    if (event.provider_id !== descriptor.provider_id || event.provider_instance !== descriptor.provider_instance) {
      throw new ProviderError(
        "invalid_response",
        "provider event identity does not match invocation",
        descriptor,
        requestId,
        { retryable: false },
      );
    }
    const previous = index === 0 ? -1 : parsed[index - 1]!.seq;
    if (event.seq <= previous) {
      throw new ProviderError(
        "invalid_response",
        "provider event sequence is not strictly increasing",
        descriptor,
        requestId,
        { retryable: false, details: { previous, actual: event.seq } },
      );
    }
  }
  if (parsed[0]?.type !== "started") {
    throw new ProviderError("invalid_response", "provider event stream must start with started", descriptor, requestId, {
      retryable: false,
    });
  }
  const terminalIndexes = parsed.flatMap((event, index) => event.type === "terminal_result" ? [index] : []);
  if (terminalIndexes.length !== 1 || terminalIndexes[0] !== parsed.length - 1) {
    throw new ProviderError(
      "invalid_response",
      "provider event stream must end with exactly one terminal_result",
      descriptor,
      requestId,
      { retryable: false },
    );
  }
  const invocationId = parsed[0]?.invocation_id;
  if (!invocationId || parsed.some((event) => event.invocation_id !== invocationId)) {
    throw new ProviderError("invalid_response", "provider event stream contains multiple invocation ids", descriptor, requestId, {
      retryable: false,
    });
  }
  return parsed;
}

export function terminalOutcome(events: readonly ProviderEventType[], descriptor: ProviderDescriptor, requestId: string): string {
  const terminal = events[events.length - 1];
  if (!terminal || terminal.type !== "terminal_result") {
    throw new ProviderError("invalid_response", "missing terminal provider event", descriptor, requestId, {
      retryable: false,
    });
  }
  const outcome = terminal.payload.outcome;
  if (typeof outcome !== "string" || !outcome) {
    throw new ProviderError("invalid_response", "terminal provider event has no typed outcome", descriptor, requestId, {
      retryable: false,
    });
  }
  return outcome;
}
