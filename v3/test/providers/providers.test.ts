import { describe, expect, test } from "bun:test";
import { ProviderDescriptor as ProviderDescriptorSchema } from "../../src/providers/types.ts";
import type { ProviderEventEnvelope, PublicProviderResponse } from "../../src/providers/index.ts";
import { NativeProvider } from "../../src/providers/native.ts";
import { HermesProvider, type HermesAcpLifecycle, type HermesAcpHandshake, type HermesPublicRequest } from "../../src/providers/hermes.ts";
import { ProviderRegistry } from "../../src/providers/registry.ts";
import { invokeCapability } from "../../src/providers/invocation.ts";
import { makeProviderEvent } from "../../src/providers/events.ts";
import type { IncidentPacket } from "../../src/providers/types.ts";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

function descriptor(providerId: "native" | "hermes" = "native") {
  return ProviderDescriptorSchema.parse({
    contract: "car.provider.v1",
    provider_id: providerId,
    provider_version: providerId === "native" ? "native-1.0.0" : "hermes-acp-1.0.0",
    capabilities: ["operator", "policy", "memory"],
    contracts: { operator: "car.operator.v1", policy: "car.policy.v1", memory: "car.memory.v1" },
    provider_instance: providerId,
    ...(providerId === "hermes" ? { profile: "work" } : {}),
    continuity: "global",
    continuity_key: "global",
    state_root: `/tmp/car-provider-${providerId}`,
    config_fingerprint: `${providerId}-fingerprint`,
  });
}

function packet(provider: ReturnType<typeof descriptor>, requestId = "req-1"): IncidentPacket {
  return {
    contract: "car.operator.v1",
    request_id: requestId,
    deadline_at: new Date(Date.now() + 2_000).toISOString(),
    provider,
    incident_id: "inc-1",
    car_session_id: "sess-1",
    events: [{ id: "evt-1", type: "attention.question", severity: "attention", title: "Proceed?", requires_response: true }],
    context: { charter: "", hits: [] },
  };
}

describe("provider contracts and native provider", () => {
  test("native provider is deterministic and has no external runtime requirement", async () => {
    const native = new NativeProvider({ descriptor: descriptor() });
    const preflight = await native.preflight();
    expect(preflight.ready).toBe(true);
    expect((await native.health()).details.external_runtime).toBe(false);
    const result = await native.decide(packet(native.descriptor));
    expect(result.contract).toBe("car.operator.v1");
    expect(result.disposition).toBe("escalate");
    expect(result.effects).toEqual([]);
  });

  test("registry separates registration from readiness", async () => {
    const registry = new ProviderRegistry();
    const native = new NativeProvider({ descriptor: descriptor() });
    registry.register(native);
    expect(registry.isReady("native")).toBe(false);
    await registry.preflight("native");
    await registry.health("native");
    expect(registry.isReady("native")).toBe(true);
    expect(() => registry.register(native)).toThrow(/already registered/);
  });

  test("invokeCapability deduplicates concurrent same request ids", async () => {
    const native = new NativeProvider({ descriptor: descriptor() });
    const input = packet(native.descriptor, "same-request");
    const [one, two] = await Promise.all([
      invokeCapability(native, "operator", input),
      invokeCapability(native, "operator", input),
    ]);
    expect(one.value).toEqual(two.value);
    expect(one.events.at(-1)?.type).toBe("terminal_result");
  });

  test("native memory observation is durably idempotent across provider recreation", async () => {
    const root = mkdtempSync(join(tmpdir(), "car-native-memory-"));
    try {
      const d = ProviderDescriptorSchema.parse({ ...descriptor(), state_root: root });
      const input = {
        contract: "car.memory.v1" as const,
        request_id: "observe-1",
        deadline_at: new Date(Date.now() + 2_000).toISOString(),
        provider: d,
        kind: "instruction" as const,
        fact_id: "fact-1",
        body: { text: "remember this" },
        incident_id: "inc-1",
      };
      await new NativeProvider({ descriptor: d }).observe(input);
      await new NativeProvider({ descriptor: d }).observe({ ...input, request_id: "observe-retry" });
      expect(JSON.parse(readFileSync(join(root, "memory.json"), "utf8"))).toHaveLength(1);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

class FakeHermes implements HermesAcpLifecycle {
  initialized = false;
  cancelled: string[] = [];
  response: PublicProviderResponse<unknown>;
  delayMs = 0;

  constructor(response: PublicProviderResponse<unknown>) {
    this.response = response;
  }

  async initialize(): Promise<HermesAcpHandshake> {
    this.initialized = true;
    return { protocol_version: "1", server_name: "hermes", capabilities: ["operator", "policy", "memory"] };
  }

  async invoke(_input: HermesPublicRequest, signal?: AbortSignal): Promise<PublicProviderResponse<unknown>> {
    if (this.delayMs > 0) await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, this.delayMs);
      signal?.addEventListener("abort", () => { clearTimeout(timer); reject(new Error("aborted")); }, { once: true });
    });
    return this.response;
  }

  async cancel(requestId: string): Promise<void> {
    this.cancelled.push(requestId);
  }
}

function hermesResponse(descriptorValue: ReturnType<typeof descriptor>, requestId: string): PublicProviderResponse<unknown> {
  const started = makeProviderEvent(descriptorValue, { invocation_id: "inv-1", request_id: requestId, type: "started", seq: 0, ts: new Date().toISOString(), payload: {} });
  const terminal = makeProviderEvent(descriptorValue, { invocation_id: "inv-1", request_id: requestId, type: "terminal_result", seq: 1, ts: new Date().toISOString(), payload: { outcome: "succeeded" } });
  return {
    result: { contract: "car.operator.v1", request_id: requestId, disposition: "keep_informed", rationale: "public result", effects: [] },
    events: [started, terminal] as ProviderEventEnvelope[],
  };
}

describe("Hermes ACP adapter", () => {
  test("uses ACP public terminal events and preserves explicit profile identity", async () => {
    const d = descriptor("hermes");
    const lifecycle = new FakeHermes(hermesResponse(d, "hermes-request"));
    const hermes = new HermesProvider({ descriptor: d, profile: "work", lifecycle });
    expect((await hermes.preflight()).ready).toBe(true);
    const result = await hermes.decide(packet(d, "hermes-request"));
    expect(result.disposition).toBe("keep_informed");
    expect(lifecycle.initialized).toBe(true);
  });

  test("rejects a public response without terminal evidence", async () => {
    const d = descriptor("hermes");
    const lifecycle = new FakeHermes({ result: hermesResponse(d, "x").result, events: [] });
    const hermes = new HermesProvider({ descriptor: d, profile: "work", lifecycle });
    await hermes.preflight();
    await expect(hermes.decide(packet(d, "missing-terminal"))).rejects.toMatchObject({ failure: { code: "invalid_response" } });
  });

  test("cancels and types an ACP deadline", async () => {
    const d = descriptor("hermes");
    const lifecycle = new FakeHermes(hermesResponse(d, "slow").result ? hermesResponse(d, "slow") : hermesResponse(d, "slow"));
    lifecycle.delayMs = 100;
    const hermes = new HermesProvider({ descriptor: d, profile: "work", lifecycle });
    await hermes.preflight();
    const p = packet(d, "slow");
    p.deadline_at = new Date(Date.now() + 10).toISOString();
    await expect(hermes.decide(p)).rejects.toMatchObject({ failure: { code: "deadline_exceeded" } });
    expect(lifecycle.cancelled).toContain("slow");
  });
});
