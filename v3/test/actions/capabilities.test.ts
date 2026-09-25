/**
 * Runtime capability probes: never trust docs, cache for 24h, and let a
 * successful probe that no longer advertises `resume` push the reply to the
 * file inbox rather than into a black hole.
 */
import { afterEach, describe, expect, test } from "bun:test";
import { readdirSync } from "node:fs";
import { join } from "node:path";
import { createActionBus } from "../../src/actions/index.ts";
import {
  CAPABILITY_TTL_MS,
  capabilityKey,
  probeCapabilities,
  supports,
  type CliCapabilities,
} from "../../src/actions/capabilities.ts";
import { repliesDir } from "../../src/config/config.ts";
import { FakeClock, memoryStore } from "../fakes.ts";
import { auditVerbs, helpResponder, makeRunner, seedSession, StubPolicy, tempState } from "./helpers.ts";

const cleanups: (() => void)[] = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

describe("probeCapabilities", () => {
  test("parses resume/queue support out of --help and caches it", async () => {
    const store = memoryStore(new FakeClock());
    const runner = makeRunner(helpResponder());

    const first = await probeCapabilities(store, runner.runner, "codex");
    expect(first.cli).toBe("codex");
    expect(first.ok).toBe(true);
    expect(first.resume).toBe(true);
    // The installed codex-cli has no `codex queue` — DESIGN §2.
    expect(first.queue).toBe(false);
    expect(runner.calls).toHaveLength(1);

    const second = await probeCapabilities(store, runner.runner, "codex");
    expect(second).toEqual(first);
    expect(runner.calls).toHaveLength(1); // served from kv
    expect(store.kvGet<CliCapabilities>(capabilityKey("codex"))?.resume).toBe(true);
  });

  test("re-probes after the 24h TTL", async () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const runner = makeRunner(helpResponder());
    await probeCapabilities(store, runner.runner, "claude-code");
    clock.advance(CAPABILITY_TTL_MS - 1000);
    await probeCapabilities(store, runner.runner, "claude-code");
    expect(runner.calls).toHaveLength(1);
    clock.advance(2000);
    await probeCapabilities(store, runner.runner, "claude-code");
    expect(runner.calls).toHaveLength(2);
  });

  test("force re-probes immediately", async () => {
    const store = memoryStore(new FakeClock());
    const runner = makeRunner(helpResponder());
    await probeCapabilities(store, runner.runner, "codex");
    await probeCapabilities(store, runner.runner, "codex", { force: true });
    expect(runner.calls).toHaveLength(2);
  });

  test("maps vendors to their executables", async () => {
    const store = memoryStore(new FakeClock());
    const runner = makeRunner(helpResponder());
    await probeCapabilities(store, runner.runner, "claude-code");
    expect(runner.calls[0]?.argv).toEqual(["claude", "--help"]);
  });

  test("a failed probe is 'unknown', not 'unsupported'", async () => {
    const store = memoryStore(new FakeClock());
    const runner = makeRunner(() => ({ code: 127, stderr: "command not found" }));
    const caps = await probeCapabilities(store, runner.runner, "codex");
    expect(caps.ok).toBe(false);
    expect(caps.resume).toBe(false);
    // Unknown → attempt anyway rather than degrading every reply on a flaky --help.
    expect(supports(caps, "resume")).toBe(true);
    expect(auditVerbs(store, "codex")).toContain("capability.probe_failed");
  });

  test("a successful probe without the verb is believed", async () => {
    const store = memoryStore(new FakeClock());
    const runner = makeRunner(() => ({ code: 0, stdout: "Usage: codex\nCommands:\n  exec" }));
    const caps = await probeCapabilities(store, runner.runner, "codex");
    expect(caps.ok).toBe(true);
    expect(supports(caps, "resume")).toBe(false);
    expect(auditVerbs(store, "codex")).toContain("capability.probed");
  });
});

describe("unsupported capability → fallback", () => {
  test("codex without resume never spawns a resume, it stages a file", async () => {
    const state = tempState();
    cleanups.push(state.cleanup);
    const store = memoryStore(new FakeClock());
    const runner = makeRunner((argv) =>
      argv[1] === "--help" ? { code: 0, stdout: "Usage: codex\nCommands:\n  exec  run once" } : undefined,
    );
    const bus = createActionBus(store, state.config, new StubPolicy(), { runner: runner.runner, env: {} });
    const sid = seedSession(store, { vendor: "codex", native_id: "uuid-1" });

    const result = await bus.deliver(sid, { kind: "codex-exec-resume" }, { text: "hi" });

    expect(result).toBe("degraded");
    expect(runner.lines()).toEqual(["codex --help"]);
    expect(readdirSync(join(repliesDir(state.config), sid))).toEqual(["reply-0001.md"]);
  });

  test("the probe is cached across deliveries", async () => {
    const state = tempState();
    cleanups.push(state.cleanup);
    const store = memoryStore(new FakeClock());
    const runner = makeRunner(helpResponder());
    const bus = createActionBus(store, state.config, new StubPolicy(), { runner: runner.runner, env: {} });
    const sid = seedSession(store, { vendor: "codex", native_id: "uuid-1" });
    await bus.deliver(sid, { kind: "codex-exec-resume" }, { text: "one" });
    await bus.deliver(sid, { kind: "codex-exec-resume" }, { text: "two" });
    expect(runner.lines().filter((l) => l.endsWith("--help"))).toEqual(["codex --help"]);
  });

  test("the bus exposes the probe for the doctor command", async () => {
    const state = tempState();
    cleanups.push(state.cleanup);
    const store = memoryStore(new FakeClock());
    const runner = makeRunner(helpResponder());
    const bus = createActionBus(store, state.config, new StubPolicy(), { runner: runner.runner, env: {} });
    const caps = await bus.probeCapabilities("agentctl");
    expect(caps.verbs).toContain("run");
    expect(caps.verbs).toContain("subscribe");
  });
});
