import { ProviderError } from "./errors.ts";
import {
  PROVIDER_CAPABILITIES,
  ProviderDescriptor,
  type CapabilityProvider,
  type ProviderCapability,
  type ProviderHealth,
  type ProviderPreflight,
} from "./types.ts";

/**
 * In-process registry. Registration is intentionally side-effect free; a
 * provider's preflight/health must pass separately before the router invokes it.
 */
export class ProviderRegistry {
  private readonly providers = new Map<string, CapabilityProvider>();
  private readonly readiness = new Map<string, ProviderPreflight>();
  private readonly healthSnapshots = new Map<string, ProviderHealth>();

  register(provider: CapabilityProvider): ProviderDescriptor {
    const descriptor = ProviderDescriptor.parse(provider.descriptor);
    if (this.providers.has(descriptor.provider_instance)) {
      throw new ProviderError(
        "duplicate_provider",
        `provider instance already registered: ${descriptor.provider_instance}`,
        descriptor,
        "registration",
        { retryable: false },
      );
    }
    const missingContracts = descriptor.capabilities.filter((capability) => {
      const expected = capability === "operator"
        ? "car.operator.v1"
        : capability === "policy"
          ? "car.policy.v1"
          : "car.memory.v1";
      return descriptor.contracts[capability] !== expected;
    });
    if (missingContracts.length > 0) {
      throw new ProviderError(
        "version_mismatch",
        `provider has missing or incompatible capability contracts: ${missingContracts.join(", ")}`,
        descriptor,
        "registration",
        { retryable: false, details: { expected_protocol: "car.provider.v1" } },
      );
    }
    for (const capability of descriptor.capabilities) {
      const method = capability === "operator" ? provider.decide : capability === "policy" ? provider.evaluate : provider.context;
      if (typeof method !== "function") {
        throw new ProviderError(
          "unsupported_capability",
          `provider advertises ${capability} but does not implement it`,
          descriptor,
          "registration",
          { retryable: false },
        );
      }
    }
    this.providers.set(descriptor.provider_instance, provider);
    return descriptor;
  }

  unregister(instanceId: string): boolean {
    this.readiness.delete(instanceId);
    this.healthSnapshots.delete(instanceId);
    return this.providers.delete(instanceId);
  }

  get(instanceId: string): CapabilityProvider | null {
    return this.providers.get(instanceId) ?? null;
  }

  require(instanceId: string, capability?: ProviderCapability): CapabilityProvider {
    const provider = this.get(instanceId);
    if (!provider) throw new Error(`unknown provider instance ${instanceId}`);
    if (capability && !provider.descriptor.capabilities.includes(capability)) {
      throw new ProviderError(
        "unsupported_capability",
        `${instanceId} does not provide ${capability}`,
        provider.descriptor,
        "registry",
        { retryable: false },
      );
    }
    return provider;
  }

  descriptors(): ProviderDescriptor[] {
    return [...this.providers.values()].map((provider) => provider.descriptor);
  }

  async preflight(instanceId?: string): Promise<Map<string, ProviderPreflight>> {
    const selected = instanceId ? [this.require(instanceId)] : [...this.providers.values()];
    const entries = await Promise.all(selected.map(async (provider) => {
      const result = await provider.preflight();
      this.readiness.set(provider.descriptor.provider_instance, result);
      return [provider.descriptor.provider_instance, result] as const;
    }));
    return new Map(entries);
  }

  async health(instanceId?: string): Promise<Map<string, ProviderHealth>> {
    const selected = instanceId ? [this.require(instanceId)] : [...this.providers.values()];
    const entries = await Promise.all(selected.map(async (provider) => {
      const result = await provider.health();
      this.healthSnapshots.set(provider.descriptor.provider_instance, result);
      return [provider.descriptor.provider_instance, result] as const;
    }));
    return new Map(entries);
  }

  /** Readiness is cached only after explicit preflight; registration is not readiness. */
  isReady(instanceId: string): boolean {
    const result = this.readiness.get(instanceId);
    return result?.ready === true && this.healthSnapshots.get(instanceId)?.healthy !== false;
  }

  assertReady(instanceId: string, requestId: string): CapabilityProvider {
    const provider = this.require(instanceId);
    if (!this.isReady(instanceId)) {
      throw new ProviderError("not_ready", `provider ${instanceId} has not passed preflight and health`, provider.descriptor, requestId, {
        retryable: true,
      });
    }
    return provider;
  }

  /** Close all providers once; callers should invoke this at daemon shutdown. */
  async close(): Promise<void> {
    const providers = [...this.providers.values()];
    this.providers.clear();
    this.readiness.clear();
    this.healthSnapshots.clear();
    await Promise.all(providers.map((provider) => provider.close?.()));
  }
}
export function assertCapability(capability: string): asserts capability is ProviderCapability {
  if (!(PROVIDER_CAPABILITIES as readonly string[]).includes(capability)) {
    throw new Error(`unsupported capability ${capability}`);
  }
}
