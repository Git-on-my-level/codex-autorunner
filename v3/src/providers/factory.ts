import type { ResolvedProvider } from "../config/provider_topology.ts";
import { descriptorFromResolved, type CapabilityProvider, type ProviderCapability } from "./types.ts";
import { NativeProvider } from "./native.ts";
import { HermesProvider, type HermesAcpLifecycle } from "./hermes.ts";

export interface ProviderFactoryOptions {
  nativeVersion?: string;
  hermesVersion?: string;
  hermesLifecycle?: HermesAcpLifecycle;
  /**
   * Combine slots that resolve to the same configured instance. The default
   * remains the single capability from `resolved`, which is useful for
   * distinct instances; the daemon should pass all slots for one instance
   * before registering it once.
   */
  capabilities?: ProviderCapability[];
}

/**
 * Construct the implementation selected by provider_topology.ts. Resolution
 * remains a pure config concern; this function is the only composition seam
 * that turns the resolved identity into a provider runtime.
 */
export function createProviderForResolution(
  resolved: ResolvedProvider,
  options: ProviderFactoryOptions = {},
): CapabilityProvider {
  const capabilities: ProviderCapability[] = options.capabilities?.length
    ? [...new Set(options.capabilities)]
    : [resolved.capability];
  const descriptor = descriptorFromResolved(
    resolved,
    resolved.provider_id === "native" ? options.nativeVersion ?? "native-1.0.0" : options.hermesVersion ?? "hermes-acp-1.0.0",
    capabilities,
  );
  if (resolved.provider_id === "native") return new NativeProvider({ descriptor });
  if (!options.hermesLifecycle) throw new Error("Hermes provider resolution requires an ACP lifecycle adapter");
  if (!resolved.profile) throw new Error("Hermes provider resolution requires an explicit profile");
  return new HermesProvider({ descriptor, profile: resolved.profile, lifecycle: options.hermesLifecycle });
}
