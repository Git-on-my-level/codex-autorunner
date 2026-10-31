import { createHash } from "node:crypto";
import { join } from "node:path";
import type { CarConfig } from "./config.ts";

export const PROVIDER_CAPABILITIES = ["operator", "policy", "memory"] as const;
export type ProviderCapability = (typeof PROVIDER_CAPABILITIES)[number];
export type ProviderScopeKey = "repo" | "host" | "source" | "policy_domain";

export interface ProviderSelectionContext {
  repo?: string;
  host?: string;
  source?: string;
  policy_domain?: string;
  incident_id?: string;
}

export interface ResolvedProvider {
  capability: ProviderCapability;
  provider_id: "native" | "hermes";
  /** User-configured instance selector from config.toml. */
  configured_instance_id: string;
  /** Runtime identity; scoped/incident instances include their continuity key. */
  instance_id: string;
  profile?: string;
  continuity: "global" | "scoped" | "incident";
  continuity_key: string;
  state_root: string;
  config_fingerprint: string;
  matched_route_index: number | null;
}

const MATCH_KEYS = new Set<ProviderScopeKey>(["repo", "host", "source", "policy_domain"]);

/** Fail closed before daemon startup; config errors never silently choose another provider. */
export function validateProviderTopology(config: CarConfig): void {
  const instances = config.providers.instances;
  for (const capability of PROVIDER_CAPABILITIES) {
    const name = config.providers.defaults[capability];
    if (!instances[name]) throw new Error(`providers.defaults.${capability} references unknown instance ${name}`);
  }

  const selectors = new Set<string>();
  config.providers.routes.forEach((route, index) => {
    for (const key of Object.keys(route.match)) {
      if (!MATCH_KEYS.has(key as ProviderScopeKey)) {
        throw new Error(`providers.routes[${index}] uses unsupported selector ${key}`);
      }
    }
    const selector = canonicalJson(route.match);
    if (selectors.has(selector)) throw new Error(`providers.routes[${index}] duplicates selector ${selector}`);
    selectors.add(selector);
    for (const capability of PROVIDER_CAPABILITIES) {
      const name = route[capability];
      if (name && !instances[name]) {
        throw new Error(`providers.routes[${index}].${capability} references unknown instance ${name}`);
      }
    }
  });

  for (const [name, instance] of Object.entries(instances)) {
    if (instance.adapter === "hermes" && !instance.profile) {
      throw new Error(`providers.instances.${name}: Hermes requires an explicit profile`);
    }
    if (instance.continuity === "scoped" && instance.scope.length === 0) {
      throw new Error(`providers.instances.${name}: scoped continuity requires at least one scope key`);
    }
  }
}

export function resolveProvider(
  config: CarConfig,
  capability: ProviderCapability,
  context: ProviderSelectionContext,
): ResolvedProvider {
  validateProviderTopology(config);
  const routeIndex = config.providers.routes.findIndex((route) => matches(route.match, context));
  const route = routeIndex >= 0 ? config.providers.routes[routeIndex] : undefined;
  const instanceId = route?.[capability] ?? config.providers.defaults[capability];
  const instance = config.providers.instances[instanceId];
  if (!instance) throw new Error(`resolved unknown ${capability} provider instance ${instanceId}`);

  const continuityKey = continuityKeyFor(instance.continuity, instance.scope, context);
  const runtimeInstanceId = instance.continuity === "global" ? instanceId : `${instanceId}@${continuityKey}`;
  const instancePath = digest(instanceId, 24);
  const stateRoot = join(
    config.state_dir,
    "providers",
    instance.adapter,
    "instances",
    instancePath,
    continuityKey,
    "state",
  );
  const fingerprint = digest(
    canonicalJson({
      instance_id: instanceId,
      instance,
      continuity_key: continuityKey,
    }),
    32,
  );
  return {
    capability,
    provider_id: instance.adapter,
    configured_instance_id: instanceId,
    instance_id: runtimeInstanceId,
    ...(instance.profile ? { profile: instance.profile } : {}),
    continuity: instance.continuity,
    continuity_key: continuityKey,
    state_root: stateRoot,
    config_fingerprint: fingerprint,
    matched_route_index: routeIndex >= 0 ? routeIndex : null,
  };
}

function matches(match: Record<string, string>, context: ProviderSelectionContext): boolean {
  return Object.entries(match).every(([key, pattern]) => {
    const actual = context[key as ProviderScopeKey];
    return actual !== undefined && glob(pattern, actual);
  });
}

function glob(pattern: string, actual: string): boolean {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${escaped}$`).test(actual);
}

function continuityKeyFor(
  continuity: "global" | "scoped" | "incident",
  scope: ProviderScopeKey[],
  context: ProviderSelectionContext,
): string {
  if (continuity === "global") return "global";
  if (continuity === "incident") {
    if (!context.incident_id) throw new Error("incident continuity requires incident_id");
    return `incident-${digest(context.incident_id, 24)}`;
  }
  const selected: Record<string, string> = {};
  for (const key of scope) {
    const value = context[key];
    if (!value) throw new Error(`scoped continuity requires ${key}`);
    selected[key] = value;
  }
  return `scope-${digest(canonicalJson(selected), 24)}`;
}

function digest(value: string, length: number): string {
  return createHash("sha256").update(value).digest("hex").slice(0, length);
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}
