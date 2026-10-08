import {
  ProviderFailure,
  type ProviderFailureCode,
  type ProviderDescriptor,
} from "./types.ts";

/** Structured provider failure; callers can persist this without parsing prose. */
export class ProviderError extends Error {
  readonly name = "ProviderError";
  readonly failure: ProviderFailure;

  constructor(
    code: ProviderFailureCode,
    message: string,
    descriptor: ProviderDescriptor,
    requestId: string,
    opts: { retryable?: boolean; details?: Record<string, unknown>; cause?: unknown } = {},
  ) {
    super(message, opts.cause === undefined ? undefined : { cause: opts.cause });
    this.failure = ProviderFailure.parse({
      contract: "car.provider-failure.v1",
      code,
      message,
      retryable: opts.retryable ?? isRetryable(code),
      provider_id: descriptor.provider_id,
      provider_instance: descriptor.provider_instance,
      request_id: requestId,
      details: opts.details ?? {},
    });
  }
}
export function isProviderError(value: unknown): value is ProviderError {
  return value instanceof ProviderError;
}

export function failureFromUnknown(
  error: unknown,
  descriptor: ProviderDescriptor,
  requestId: string,
): ProviderFailure {
  if (error instanceof ProviderError) return error.failure;
  const message = error instanceof Error ? error.message : String(error);
  return new ProviderError("unknown", message, descriptor, requestId, {
    retryable: true,
    cause: error,
  }).failure;
}

export function isRetryable(code: ProviderFailureCode): boolean {
  return new Set<ProviderFailureCode>([
    "deadline_exceeded",
    "transport_unavailable",
    "transport_protocol",
    "state_unavailable",
    "unknown",
  ]).has(code);
}
