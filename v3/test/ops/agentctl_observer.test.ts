import { describe, expect, test } from "bun:test";
import { createAgentctlObserverLoop, observeRun, parseRecent, resolveAgentctlExecutable } from "../../src/ops/agentctl_observer.ts";
import { FakeClock, memoryStore, testConfig } from "../fakes.ts";

describe("agentctl observer", () => {
  test("disabled observation makes no agentctl calls", async () => {
    const store = memoryStore();
    let calls = 0;
    const loop = createAgentctlObserverLoop(store, testConfig(), async () => {
      calls++;
      return { executions: [], hasMore: false };
    });
    await loop.start();
    await loop.stop();
    expect(calls).toBe(0);
    expect(store.kvGet<{ state: string }>("agentctl.observer.health")?.state).toBe("disabled");
  });

  test("discovers compact active and recent rows without progress events", async () => {
    const store = memoryStore();
    const filters: string[] = [];
    const loop = createAgentctlObserverLoop(
      store,
      testConfig({ agentctl_observer: { enabled: true, observe_all: true } }),
      async (filter) => {
        filters.push(filter);
        return filter === "nonterminal"
          ? { executions: [{ id: "exec-cursor", adapter: "cursor", state: "running", liveness: "healthy", updated_at: "2026-08-26T12:00:00Z", duration_seconds: 25 }], hasMore: false }
          : { executions: [{ id: "exec-omp", adapter: "omp", state: "completed", liveness: "exited", updated_at: "2026-08-26T11:59:00Z", terminal_at: "2026-08-26T11:59:00Z", duration_seconds: 9 }], hasMore: true };
      },
    );
    await loop.start();
    await loop.stop();

    expect(filters.sort()).toEqual(["nonterminal", "recent"]);
    expect(store.getAgentRun("exec-cursor")?.agent).toBe("cursor");
    expect(store.getAgentRun("exec-omp")?.state).toBe("completed");
    expect((store.db.query("SELECT COUNT(*) n FROM events").get() as { n: number }).n).toBe(0);
    const health = store.kvGet<{ coverage_degraded: boolean; history_truncated: boolean }>("agentctl.observer.health");
    expect(health?.coverage_degraded).toBe(false);
    expect(health?.history_truncated).toBe(true);
  });

  test("unreachable nonterminal runs are visibly stale", () => {
    const store = memoryStore();
    observeRun(store, {
      id: "exec-stale",
      adapter: "codex",
      state: "running",
      liveness: "unreachable",
      updated_at: "2026-08-26T11:00:00Z",
    });
    expect(store.getAgentRun("exec-stale")?.observation_state).toBe("stale");
  });

  test("machine labels stay in metadata instead of becoming the human title", () => {
    const store = memoryStore();
    observeRun(store, {
      id: "exec-labeled",
      adapter: "omp",
      labels: ["car-v3-dogfood", "omp"],
      state: "completed",
      liveness: "exited",
      updated_at: "2026-08-26T12:00:00Z",
    });
    expect(store.getAgentRun("exec-labeled")?.title).toBe("OMP run");
    expect(store.getAgentRun("exec-labeled")?.labels_json).toBe('["car-v3-dogfood","omp"]');
  });

  test("a descriptive exact label becomes the compact human title", () => {
    const store = memoryStore();
    observeRun(store, {
      id: "exec-labeled-title",
      adapter: "cursor",
      labels: ["car-observe", "dogfood-round3", "runs-live-truth"],
      state: "running",
      liveness: "alive",
      updated_at: "2026-08-26T12:00:00Z",
    });
    expect(store.getAgentRun("exec-labeled-title")?.title).toBe("Runs Live Truth");
  });

  test("a vanished active run becomes unknown after a complete active poll", async () => {
    const store = memoryStore();
    store.upsertAgentRun({ executionId: "exec-vanished", agent: "cursor", state: "running", liveness: "alive", updatedAt: "2026-08-26T11:59:00Z" });
    const loop = createAgentctlObserverLoop(
      store,
      testConfig({ agentctl_observer: { enabled: true, observe_all: true } }),
      async () => ({ executions: [], hasMore: false }),
    );
    await loop.start();
    await loop.stop();
    expect(store.getAgentRun("exec-vanished")?.observation_state).toBe("unknown");
  });

  test("incomplete active coverage never marks unseen runs unknown", async () => {
    const store = memoryStore();
    store.upsertAgentRun({ executionId: "exec-outside-window", agent: "omp", state: "running", liveness: "alive", updatedAt: "2026-08-26T11:59:00Z" });
    const loop = createAgentctlObserverLoop(
      store,
      testConfig({ agentctl_observer: { enabled: true, observe_all: true } }),
      async (filter) => ({ executions: [], hasMore: filter === "nonterminal" }),
    );
    await loop.start();
    await loop.stop();
    expect(store.getAgentRun("exec-outside-window")?.observation_state).toBe("observed");
  });

  test("the freshest concurrent snapshot wins before persistence", async () => {
    const store = memoryStore();
    const loop = createAgentctlObserverLoop(
      store,
      testConfig({ agentctl_observer: { enabled: true, observe_all: true } }),
      async (filter) => filter === "nonterminal"
        ? { executions: [{ id: "exec-race", adapter: "cursor", state: "running", liveness: "alive", updated_at: "2026-08-26T12:00:00Z" }], hasMore: false }
        : { executions: [{ id: "exec-race", adapter: "cursor", state: "starting", liveness: "unknown", updated_at: "2026-08-26T11:59:00Z" }], hasMore: false },
    );
    await loop.start();
    await loop.stop();
    expect(store.getAgentRun("exec-race")?.state).toBe("running");
  });

  test("a run created between snapshots is not flipped unknown", async () => {
    const store = memoryStore();
    const loop = createAgentctlObserverLoop(
      store,
      testConfig({ agentctl_observer: { enabled: true, observe_all: true } }),
      async (filter) => filter === "nonterminal"
        ? { executions: [], hasMore: false }
        : { executions: [{ id: "exec-between", adapter: "cursor", state: "running", liveness: "alive", updated_at: "2026-08-26T12:00:00Z" }], hasMore: false },
    );
    await loop.start();
    await loop.stop();
    expect(store.getAgentRun("exec-between")?.observation_state).toBe("observed");
  });

  test("one malformed execution degrades health without hiding valid rows", async () => {
    const store = memoryStore();
    const loop = createAgentctlObserverLoop(
      store,
      testConfig({ agentctl_observer: { enabled: true, observe_all: true } }),
      async (filter) => filter === "nonterminal"
        ? { executions: [
            { id: "exec-valid", adapter: "omp", state: "running", liveness: "alive", updated_at: "2026-08-26T12:00:00Z" },
            { id: "exec-invalid", adapter: "cursor", state: "", liveness: "unknown", updated_at: "2026-08-26T12:00:00Z" },
          ], hasMore: false }
        : { executions: [], hasMore: false },
    );
    await loop.start();
    await loop.stop();
    expect(store.getAgentRun("exec-valid")?.state).toBe("running");
    expect(store.kvGet<{ state: string }>("agentctl.observer.health")?.state).toBe("degraded");
  });

  test("projection cleanup removes only old terminal rows", async () => {
    const clock = new FakeClock(new Date("2026-08-26T12:00:00Z"));
    const store = memoryStore(clock);
    store.upsertAgentRun({ executionId: "exec-old-terminal", agent: "cursor", state: "completed", liveness: "exited", updatedAt: "2026-06-01T12:00:00Z", terminalAt: "2026-06-01T12:00:00Z" });
    store.upsertAgentRun({ executionId: "exec-old-active", agent: "omp", state: "running", liveness: "alive", updatedAt: "2026-06-01T12:00:00Z" });
    const loop = createAgentctlObserverLoop(
      store,
      testConfig({ agentctl_observer: { enabled: true, observe_all: true, retention_days: 30 } }),
      async (filter) => filter === "nonterminal"
        ? { executions: [{ id: "exec-old-active", adapter: "omp", state: "running", liveness: "alive", updated_at: "2026-08-26T12:00:00Z" }], hasMore: false }
        : { executions: [], hasMore: false },
    );
    await loop.start();
    await loop.stop();
    expect(store.getAgentRun("exec-old-terminal")).toBeNull();
    expect(store.getAgentRun("exec-old-active")?.state).toBe("running");
  });

  test("explicit agentctl executable paths remain caller-authoritative", () => {
    expect(resolveAgentctlExecutable("/opt/tools/agentctl")).toBe("/opt/tools/agentctl");
  });

  test("parses the public recent envelope and preserves truncation evidence", () => {
    expect(parseRecent(JSON.stringify({ ok: true, result: { executions: [], has_more: true } }))).toEqual({ executions: [], hasMore: true });
    expect(() => parseRecent(JSON.stringify({ ok: true, result: { executions: [] } }))).toThrow(/invalid envelope/);
    expect(() => parseRecent(JSON.stringify({ ok: false }))).toThrow(/invalid envelope/);
  });
});
