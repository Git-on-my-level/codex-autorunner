import { describe, expect, test } from "bun:test";
import { CarConfig } from "../../src/config/config.ts";
import { resolveProvider } from "../../src/config/provider_topology.ts";
import { createProviderHost } from "../../src/providers/host.ts";
import type { AcpSessionBindings } from "../../src/providers/acp_stdio.ts";
import type { HermesAcpLifecycle } from "../../src/providers/hermes.ts";
import { memoryStore } from "../fakes.ts";

class StubHermesLifecycle implements HermesAcpLifecycle {
  async initialize(): Promise<{ protocol_version: string; capabilities: string[] }> {
    return { protocol_version: "1", capabilities: [] };
  }
  async invoke(): Promise<never> {
    throw new Error("not used");
  }
  async cancel(): Promise<void> {}
  async close(): Promise<void> {}
}

describe("ProviderHost", () => {
  test("single-flights one global native runtime and exposes all three slots", async () => {
    const config = CarConfig.parse({ state_dir: "/tmp/car-provider-host" });
    const store = memoryStore();
    const host = createProviderHost(store, config);
    const context = { source: "generic", host: "test-host" };
    const operator = resolveProvider(config, "operator", context);
    const [a, b] = await Promise.all([
      host.ensure(operator, "operator"),
      host.ensure(resolveProvider(config, "memory", context), "memory"),
    ]);

    expect(a).toBe(b);
    expect(a.descriptor.capabilities).toEqual(["operator", "policy", "memory"]);
    expect(host.registry.descriptors()).toHaveLength(1);
    await host.close();
  });

  test("keeps scoped provider runtimes isolated by continuity identity", async () => {
    const config = CarConfig.parse({
      state_dir: "/tmp/car-provider-host",
      providers: {
        instances: { native: { adapter: "native", continuity: "scoped", scope: ["repo"] } },
      },
    });
    const store = memoryStore();
    const host = createProviderHost(store, config);
    const one = resolveProvider(config, "operator", { repo: "github.com/acme/one" });
    const two = resolveProvider(config, "operator", { repo: "github.com/acme/two" });
    expect(one.instance_id).not.toBe(two.instance_id);
    expect(await host.ensure(one, "operator")).not.toBe(await host.ensure(two, "operator"));
    expect(host.registry.descriptors()).toHaveLength(2);
    await host.close();
  });

  test("backs Hermes ACP session bindings with canonical Store KV across host recreation", async () => {
    const config = CarConfig.parse({
      state_dir: "/tmp/car-provider-host",
      providers: {
        defaults: { operator: "hermes", policy: "hermes", memory: "hermes" },
        instances: { hermes: { adapter: "hermes", profile: "work", continuity: "global", scope: [] } },
      },
    });
    const store = memoryStore();
    const resolved = resolveProvider(config, "operator", { source: "telegram" });
    let bindings: AcpSessionBindings | undefined;
    const factory = (_resolved: typeof resolved, _executable: string | undefined, sessionBindings?: AcpSessionBindings): HermesAcpLifecycle => {
      bindings = sessionBindings;
      return new StubHermesLifecycle();
    };
    const first = createProviderHost(store, config, { hermesLifecycle: factory });
    await first.ensure(resolved, "operator");
    expect(bindings).toBeDefined();
    await bindings!.set(resolved.instance_id, resolved.config_fingerprint, resolved.continuity_key, "hermes-session-1");
    await first.close();

    const second = createProviderHost(store, config, { hermesLifecycle: factory });
    await second.ensure(resolved, "operator");
    expect(await bindings!.get(resolved.instance_id, resolved.config_fingerprint, resolved.continuity_key)).toBe("hermes-session-1");
    await second.close();
  });
});
