import { describe, expect, test } from "bun:test";
import { CarConfig } from "../../src/config/config.ts";
import { resolveProvider, validateProviderTopology } from "../../src/config/provider_topology.ts";
import { testConfig } from "../fakes.ts";

function topology() {
  return testConfig({
    providers: {
      defaults: { operator: "native", policy: "native", memory: "native" },
      instances: {
        native: { adapter: "native", continuity: "scoped", scope: ["repo"] },
        "hermes:work": { adapter: "hermes", profile: "work", continuity: "global" },
        "hermes:incident": { adapter: "hermes", profile: "clean", continuity: "incident" },
      },
      routes: [
        {
          match: { repo: "github.com/acme/*" },
          operator: "hermes:work",
          memory: "hermes:work",
        },
      ],
    },
  });
}

describe("provider topology", () => {
  test("the out-of-box native provider routes sessionless events", () => {
    const config = CarConfig.parse({ state_dir: "/tmp/car-provider-test" });
    const resolved = resolveProvider(config, "operator", { source: "generic", host: "test-host" });
    const policy = resolveProvider(config, "policy", { source: "generic", host: "test-host" });
    expect(resolved).toMatchObject({
      provider_id: "native",
      instance_id: "native",
      continuity: "global",
      continuity_key: "global",
    });
    expect(policy.config_fingerprint).toBe(resolved.config_fingerprint);
  });

  test("uses ordered routes and records explicit global Hermes identity", () => {
    const resolved = resolveProvider(topology(), "operator", { repo: "github.com/acme/widget" });
    expect(resolved).toMatchObject({
      provider_id: "hermes",
      instance_id: "hermes:work",
      profile: "work",
      continuity_key: "global",
      matched_route_index: 0,
    });
    expect(resolved.state_root).not.toContain("github.com/acme/widget");
  });

  test("native scoped state is isolated without leaking repo names into paths", () => {
    const a = resolveProvider(topology(), "policy", { repo: "github.com/other/a" });
    const b = resolveProvider(topology(), "policy", { repo: "github.com/other/b" });
    expect(a.configured_instance_id).toBe("native");
    expect(a.instance_id).not.toBe(b.instance_id);
    expect(a.instance_id).toStartWith("native@scope-");
    expect(a.continuity_key).not.toBe(b.continuity_key);
    expect(a.state_root).not.toContain("github.com");
  });

  test("incident continuity fails closed without an incident id", () => {
    const config = topology();
    config.providers.defaults.operator = "hermes:incident";
    expect(() => resolveProvider(config, "operator", { repo: "github.com/other/a" })).toThrow(
      "incident continuity requires incident_id",
    );
  });

  test("unknown instances, duplicate selectors, and implicit Hermes profiles reject config", () => {
    const unknown = topology();
    unknown.providers.defaults.operator = "missing";
    expect(() => validateProviderTopology(unknown)).toThrow("unknown instance");

    const duplicate = topology();
    duplicate.providers.routes.push({
      match: { repo: "github.com/acme/*" },
      operator: "native",
    });
    expect(() => validateProviderTopology(duplicate)).toThrow("duplicates selector");

    const noProfile = topology();
    noProfile.providers.instances["hermes:work"]!.profile = undefined;
    expect(() => validateProviderTopology(noProfile)).toThrow("explicit profile");
  });
});
