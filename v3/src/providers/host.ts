import type { CarConfig } from "../config/config.ts";
import type { ResolvedProvider } from "../config/provider_topology.ts";
import type { Store } from "../store/db.ts";
import { createHash } from "node:crypto";
import { createAcpStdioLifecycle, type AcpSessionBindings } from "./acp_stdio.ts";
import { createProviderForResolution } from "./factory.ts";
import type { HermesAcpLifecycle } from "./hermes.ts";
import { ProviderRegistry } from "./registry.ts";
import type { CapabilityProvider, ProviderCapability } from "./types.ts";

const ALL_CAPABILITIES: ProviderCapability[] = ["operator", "policy", "memory"];

export interface ProviderHostOptions {
  registry?: ProviderRegistry;
  hermesLifecycle?: (resolved: ResolvedProvider, executable?: string, sessionBindings?: AcpSessionBindings) => HermesAcpLifecycle;
}

/** Stable, bounded KV key for one provider/config/continuity identity. */
export function providerSessionBindingKey(providerInstance: string, configFingerprint: string, continuityKey: string): string {
  const identity = JSON.stringify([providerInstance, configFingerprint, continuityKey]);
  return `provider.acp.session.v1:${createHash("sha256").update(identity).digest("hex")}`;
}

/** Adapt the canonical Store KV repository to the ACP lifecycle seam. */
export function createProviderSessionBindings(store: Store): AcpSessionBindings {
  return {
    get: (providerInstance, configFingerprint, continuityKey) => store.kvGet<string>(providerSessionBindingKey(providerInstance, configFingerprint, continuityKey)),
    set: (providerInstance, configFingerprint, continuityKey, sessionId) => store.kvSet(providerSessionBindingKey(providerInstance, configFingerprint, continuityKey), sessionId),
    delete: (providerInstance, configFingerprint, continuityKey) => store.kvDelete(providerSessionBindingKey(providerInstance, configFingerprint, continuityKey)),
  };
}

/**
 * Single-flight runtime owner for dynamically resolved provider scopes.
 *
 * Config names are not runtime identities: scoped and incident resolutions
 * carry distinct instance ids. The host owns exactly one implementation and,
 * for Hermes, one ACP lifecycle for each such runtime identity.
 */
export class ProviderHost {
  readonly registry: ProviderRegistry;
  private readonly starting = new Map<string, Promise<CapabilityProvider>>();
  private readonly lifecycleFactory: NonNullable<ProviderHostOptions["hermesLifecycle"]>;
  private readonly sessionBindings: AcpSessionBindings;

  constructor(
    private readonly store: Store,
    private readonly config: CarConfig,
    options: ProviderHostOptions = {},
  ) {
    this.registry = options.registry ?? new ProviderRegistry();
    const sessionBindings = createProviderSessionBindings(store);
    this.lifecycleFactory = options.hermesLifecycle ?? ((resolved, executable, bindings = sessionBindings) => createAcpStdioLifecycle({
      executable: executable ?? "hermes",
      profile: resolved.profile,
      sessionBindings: bindings,
    }));
    this.sessionBindings = sessionBindings;
  }

  async ensure(resolved: ResolvedProvider, capability: ProviderCapability): Promise<CapabilityProvider> {
    const inflight = this.starting.get(resolved.instance_id);
    if (inflight) {
      const provider = await inflight;
      return this.registry.require(provider.descriptor.provider_instance, capability);
    }
    const existing = this.registry.get(resolved.instance_id);
    if (existing) {
      if (!this.registry.isReady(resolved.instance_id)) {
        throw new Error(`provider ${resolved.instance_id} is registered but not ready`);
      }
      return this.registry.require(resolved.instance_id, capability);
    }

    const promise = this.start(resolved);
    this.starting.set(resolved.instance_id, promise);
    try {
      const provider = await promise;
      return this.registry.require(provider.descriptor.provider_instance, capability);
    } finally {
      this.starting.delete(resolved.instance_id);
    }
  }

  async refreshHealth(): Promise<void> {
    const snapshots = await this.registry.health();
    for (const [instance, health] of snapshots) {
      this.store.audit("daemon", health.healthy ? "provider.healthy" : "provider.unhealthy", "provider", instance, {
        semantic_progress_at: health.semantic_progress_at ?? null,
        details: health.details,
      });
    }
  }

  async close(): Promise<void> {
    await this.registry.close();
  }

  private async start(resolved: ResolvedProvider): Promise<CapabilityProvider> {
    const configured = this.config.providers.instances[resolved.configured_instance_id];
    if (!configured) throw new Error(`unknown configured provider ${resolved.configured_instance_id}`);
    const lifecycle = resolved.provider_id === "hermes"
      ? this.lifecycleFactory(resolved, configured.executable, this.sessionBindings)
      : undefined;
    const provider = createProviderForResolution(resolved, {
      capabilities: ALL_CAPABILITIES,
      ...(lifecycle ? { hermesLifecycle: lifecycle } : {}),
    });
    this.registry.register(provider);
    const preflight = (await this.registry.preflight(resolved.instance_id)).get(resolved.instance_id);
    const health = (await this.registry.health(resolved.instance_id)).get(resolved.instance_id);
    const ready = preflight?.ready === true && health?.healthy === true;
    this.store.audit("daemon", ready ? "provider.ready" : "provider.not_ready", "provider", resolved.instance_id, {
      configured_instance_id: resolved.configured_instance_id,
      provider_id: resolved.provider_id,
      profile: resolved.profile ?? null,
      continuity: resolved.continuity,
      continuity_key: resolved.continuity_key,
      config_fingerprint: resolved.config_fingerprint,
      diagnostics: preflight?.diagnostics ?? [],
      health: health ?? null,
    });
    if (!ready) {
      this.registry.unregister(resolved.instance_id);
      await provider.close?.();
      throw new Error(`provider ${resolved.instance_id} failed readiness`);
    }
    return provider;
  }
}

export function createProviderHost(store: Store, config: CarConfig, options: ProviderHostOptions = {}): ProviderHost {
  return new ProviderHost(store, config, options);
}
