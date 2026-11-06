import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AcpStdioError, AcpStdioLifecycle, type AcpSessionBindings } from "../../src/providers/acp_stdio.ts";
import { HermesProvider } from "../../src/providers/hermes.ts";
import { IncidentPacketSchema, OperatorDecision, ProviderDescriptor as ProviderDescriptorSchema } from "../../src/providers/types.ts";
import type { HermesPublicRequest } from "../../src/providers/hermes.ts";

const FAKE_ACP = `#!/usr/bin/env bun
let buffer = "";
const mode = process.env.CAR_FAKE_ACP_MODE || "normal";
const sessionPrefix = process.env.CAR_FAKE_ACP_SESSION_PREFIX || "fake-session";
const sessions = new Set();
function send(value) { process.stdout.write(JSON.stringify(value) + "\\n"); }
function handle(message) {
  if (message.method === "initialize") {
    send({ id: message.id, result: { protocolVersion: 1, serverInfo: { name: "fake-hermes", version: "test" }, capabilities: { sessionCapabilities: { list: true } } } });
    return;
  }
  if (message.method === "initialized") return;
  if (message.method === "session/new") {
    if (mode === "load-only") {
      send({ id: message.id, error: { code: -32099, message: "session/new must not be used" } });
      return;
    }
    const id = sessionPrefix + "-" + (sessions.size + 1);
    sessions.add(id);
    send({ id: message.id, result: { sessionId: id } });
    return;
  }
  if (message.method === "session/load") {
    if (mode === "missing-session") {
      send({ id: message.id, error: { code: -32004, message: "session not found" } });
    } else if (mode === "load-error") {
      send({ id: message.id, error: { code: -32001, message: "backend unavailable" } });
    } else if (mode === "load-only") {
      send({ id: message.id, result: { sessionId: message.params.sessionId } });
    } else {
      send({ id: message.id, result: null });
    }
    return;
  }
  if (message.method === "session/cancel") {
    process.stderr.write("cancel received\\n");
    return;
  }
  if (message.method === "session/prompt") {
    if (mode === "malformed") { process.stdout.write("not-json\\n"); return; }
    if (mode === "no-structured") { send({ id: message.id, result: { status: "completed" } }); return; }
    if (mode === "slow") { setTimeout(() => send({ id: message.id, result: { status: "completed", finalOutput: JSON.stringify({ contract: "car.operator.v1", request_id: "slow", disposition: "keep_informed", rationale: "late", effects: [] }) } }), 500); return; }
    const sessionId = message.params.sessionId;
    send({ method: "session/update", params: { sessionId, update: { content: { type: "text", text: "working" } } } });
    const prompt = message.params.prompt[0].text;
    const request = JSON.parse(prompt);
    send({ id: message.id, result: { status: "completed", finalOutput: JSON.stringify({ contract: request.response_contract, request_id: request.request_id, disposition: "keep_informed", rationale: "fake", effects: [] }) } });
    return;
  }
  if (message.method === "shutdown") { send({ id: message.id, result: null }); return; }
  if (message.method === "exit") { process.exit(0); }
  if (message.id !== undefined) send({ id: message.id, error: { code: -32601, message: "not found" } });
}
(async () => {
  for await (const chunk of Bun.stdin.stream()) {
    buffer += new TextDecoder().decode(chunk);
    const lines = buffer.split("\\n");
    buffer = lines.pop() || "";
    for (const line of lines) if (line.trim()) handle(JSON.parse(line));
  }
})();
`;

function descriptor() {
  return ProviderDescriptorSchema.parse({
    contract: "car.provider.v1",
    provider_id: "hermes",
    provider_version: "hermes-acp-1.0.0",
    capabilities: ["operator", "policy", "memory"],
    contracts: { operator: "car.operator.v1", policy: "car.policy.v1", memory: "car.memory.v1" },
    provider_instance: "hermes:work",
    profile: "work",
    continuity: "scoped",
    continuity_key: "scope-repo",
    state_root: "/tmp/car-hermes-state",
    config_fingerprint: "fingerprint",
  });
}

function request(d = descriptor(), requestId = "request-1"): HermesPublicRequest {
  return {
    contract: "car.hermes-request.v1",
    request_id: requestId,
    capability: "operator",
    deadline_at: new Date(Date.now() + 2_000).toISOString(),
    provider_instance: d.provider_instance,
    profile: "work",
    continuity_key: d.continuity_key,
    payload: {
      contract: "car.operator.v1",
      request_id: requestId,
      deadline_at: new Date(Date.now() + 2_000).toISOString(),
      provider: d,
      incident_id: "inc-1",
      car_session_id: "sess-1",
      events: [{ id: "evt-1", type: "attention.question", severity: "attention", title: "Proceed?" }],
      context: { charter: "", hits: [] },
    },
  };
}

let dir = "";
let executable = "";

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "car-acp-stdio-"));
  executable = join(dir, "fake-hermes");
  writeFileSync(executable, FAKE_ACP, { mode: 0o700 });
  chmodSync(executable, 0o700);
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("AcpStdioLifecycle", () => {
  test("rejects unknown request and response fields at the provider contract boundary", () => {
    const d = descriptor();
    const p = request(d).payload;
    expect(() => IncidentPacketSchema.parse({ ...p, unexpected: true })).toThrow();
    expect(() => OperatorDecision.parse({
      contract: "car.operator.v1",
      request_id: "r",
      disposition: "resolve",
      rationale: "ok",
      effects: [],
      unexpected: true,
    })).toThrow();
  });

  test("initializes exact hermes acp argv and returns structured public result", async () => {
    const lifecycle = new AcpStdioLifecycle({ executable });
    expect(lifecycle.argv).toEqual([executable, "acp"]);
    const d = descriptor();
    const handshake = await lifecycle.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    expect(handshake.protocol_version).toBe("1");
    const result = await lifecycle.invoke(request(d));
    expect(result.result).toMatchObject({ contract: "car.operator.v1", disposition: "keep_informed" });
    expect((result.events as { type: string }[]).at(-1)?.type).toBe("terminal_result");
    await lifecycle.close();
  });

  test("uses Hermes public profile selection for an explicit profile", () => {
    const lifecycle = new AcpStdioLifecycle({ executable, profile: "work" });
    expect(lifecycle.argv).toEqual([executable, "-p", "work", "acp"]);
  });

  test("reuses the durable ACP session binding after lifecycle recreation", async () => {
    const values = new Map<string, string>();
    const key = (providerInstance: string, configFingerprint: string, continuityKey: string) => `${providerInstance}:${configFingerprint}:${continuityKey}`;
    const sessionBindings: AcpSessionBindings = {
      get: (providerInstance, configFingerprint, continuityKey) => values.get(key(providerInstance, configFingerprint, continuityKey)) ?? null,
      set: (providerInstance, configFingerprint, continuityKey, sessionId) => {
        values.set(key(providerInstance, configFingerprint, continuityKey), sessionId);
      },
      delete: (providerInstance, configFingerprint, continuityKey) => {
        values.delete(key(providerInstance, configFingerprint, continuityKey));
      },
    };
    const d = descriptor();
    const first = new AcpStdioLifecycle({ executable, sessionBindings, env: { CAR_FAKE_ACP_SESSION_PREFIX: "first" } });
    await first.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    await first.invoke(request(d, "first-request"));
    expect(values.get(key(d.provider_instance, d.config_fingerprint, d.continuity_key))).toBe("first-1");
    await first.close();

    // This process rejects session/new, so success proves the recreated
    // lifecycle loaded the CAR-owned binding rather than starting over.
    const second = new AcpStdioLifecycle({ executable, sessionBindings, env: { CAR_FAKE_ACP_MODE: "load-only", CAR_FAKE_ACP_SESSION_PREFIX: "second" } });
    await second.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    await expect(second.invoke(request(d, "second-request"))).resolves.toMatchObject({ result: { contract: "car.operator.v1" } });
    expect(values.get(key(d.provider_instance, d.config_fingerprint, d.continuity_key))).toBe("first-1");
    await second.close();
  });

  test("replaces only an explicitly missing remote session and preserves binding on other errors", async () => {
    const values = new Map<string, string>();
    const key = (providerInstance: string, configFingerprint: string, continuityKey: string) => `${providerInstance}:${configFingerprint}:${continuityKey}`;
    const sessionBindings: AcpSessionBindings = {
      get: (providerInstance, configFingerprint, continuityKey) => values.get(key(providerInstance, configFingerprint, continuityKey)) ?? null,
      set: (providerInstance, configFingerprint, continuityKey, sessionId) => {
        values.set(key(providerInstance, configFingerprint, continuityKey), sessionId);
      },
      delete: (providerInstance, configFingerprint, continuityKey) => {
        values.delete(key(providerInstance, configFingerprint, continuityKey));
      },
    };
    const d = descriptor();
    const seed = new AcpStdioLifecycle({ executable, sessionBindings, env: { CAR_FAKE_ACP_SESSION_PREFIX: "seed" } });
    await seed.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    await seed.invoke(request(d, "seed-request"));
    await seed.close();

    const missing = new AcpStdioLifecycle({ executable, sessionBindings, env: { CAR_FAKE_ACP_MODE: "missing-session", CAR_FAKE_ACP_SESSION_PREFIX: "replacement" } });
    await missing.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    await missing.invoke(request(d, "replacement-request"));
    expect(values.get(key(d.provider_instance, d.config_fingerprint, d.continuity_key))).toBe("replacement-1");
    await missing.close();

    const failed = new AcpStdioLifecycle({ executable, sessionBindings, env: { CAR_FAKE_ACP_MODE: "load-error", CAR_FAKE_ACP_SESSION_PREFIX: "must-not-create" } });
    await failed.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    await expect(failed.invoke(request(d, "transport-error-request"))).rejects.toMatchObject({ code: "remote_error" });
    expect(values.get(key(d.provider_instance, d.config_fingerprint, d.continuity_key))).toBe("replacement-1");
    await failed.close();
  });

  test("converts deadline into cancellation and a typed transport error", async () => {
    const lifecycle = new AcpStdioLifecycle({ executable, env: { CAR_FAKE_ACP_MODE: "slow" } });
    const d = descriptor();
    await lifecycle.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    const p = request(d, "slow");
    p.deadline_at = new Date(Date.now() + 30).toISOString();
    await expect(lifecycle.invoke(p)).rejects.toMatchObject({ code: "deadline_exceeded" });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(lifecycle.getStderrTail()).toContain("cancel received");
    await lifecycle.close();
  });

  test("rejects malformed stdout and an unstructured prompt response", async () => {
    const malformed = new AcpStdioLifecycle({ executable, env: { CAR_FAKE_ACP_MODE: "malformed" } });
    const d = descriptor();
    await malformed.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    await expect(malformed.invoke(request(d, "malformed"))).rejects.toMatchObject({ code: "protocol_error" });
    await malformed.close();

    const noStructured = new AcpStdioLifecycle({ executable, env: { CAR_FAKE_ACP_MODE: "no-structured" } });
    await noStructured.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    await expect(noStructured.invoke(request(d, "no-structured"))).rejects.toMatchObject({ code: "protocol_error" });
    await noStructured.close();
  });

  test("HermesProvider surfaces ACP transport failures without private-state fallback", async () => {
    const lifecycle = new AcpStdioLifecycle({ executable, env: { CAR_FAKE_ACP_MODE: "no-structured" } });
    const d = descriptor();
    const provider = new HermesProvider({ descriptor: d, profile: "work", lifecycle });
    expect((await provider.preflight()).ready).toBe(true);
    await expect(provider.decide(request(d, "provider-no-structured").payload as never)).rejects.toMatchObject({ failure: { code: "transport_protocol" } });
    await lifecycle.close();
  });

  test("close performs ACP shutdown and exits the owned process", async () => {
    const lifecycle = new AcpStdioLifecycle({ executable });
    const d = descriptor();
    await lifecycle.initialize({ provider_instance: d.provider_instance, profile: "work", state_root: d.state_root, config_fingerprint: d.config_fingerprint });
    expect(lifecycle.getProcessId()).not.toBeNull();
    await lifecycle.close();
    expect(lifecycle.getProcessId()).toBeNull();
  });
});
