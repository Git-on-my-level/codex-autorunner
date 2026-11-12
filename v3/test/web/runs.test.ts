import { describe, expect, test } from "bun:test";
import { buildDeps, mountApp, WEB_AUTH_HEADERS, WEB_TEST_TOKEN } from "./helpers.ts";
import { FakeClock, memoryStore, testConfig } from "../fakes.ts";

const authenticatedRead = (deps: Parameters<typeof mountApp>[0], path: string) =>
  mountApp(deps).request(path, { headers: WEB_AUTH_HEADERS });
const privateReadConfig = (overrides: Record<string, unknown>) => testConfig({
  ...overrides,
  http: { private_reads: true, ingest_tokens: { web: WEB_TEST_TOKEN } },
});

describe("web runs", () => {
  test("renders a compact native-agent run model without guessed metadata", async () => {
    const deps = buildDeps({ config: undefined });
    deps.store.upsertAgentRun({
      executionId: "exec-cursor-1",
      agent: "cursor",
      state: "running",
      liveness: "healthy",
      title: "Audit settings flow",
      repo: "github.com/acme/app",
      startedAt: "2026-08-26T11:55:00Z",
      updatedAt: "2026-08-26T12:00:00Z",
      durationSeconds: 300,
    });
    deps.store.upsertAgentRun({
      executionId: "exec-omp-1",
      agent: "omp",
      state: "running",
      liveness: "unreachable",
      title: "Check mobile layout",
      updatedAt: "2026-08-26T11:00:00Z",
      durationSeconds: 3600,
      observationState: "stale",
    });

    const res = await authenticatedRead(deps, "/ui/runs");
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("Audit settings flow");
    expect(body).toContain(">Cursor<");
    expect(body).toContain(">OMP<");
    expect(body).toContain("Last seen Running");
    expect(body).not.toContain("Not reported");
    expect(body).toContain("Local observation is off");
    expect(body).toContain(">0</span><span class=\"overview-label\">active");
    expect(body).toContain(">2</span><span class=\"overview-label\">need attention");
  });

  test("surfaces degraded observer health", async () => {
    const deps = buildDeps({ config: privateReadConfig({ agentctl_observer: { enabled: true, observe_all: true } }) });
    deps.store.kvSet("agentctl.observer.health", { state: "degraded", observed_at: "2026-08-26T12:00:00Z", error: "agentctl unavailable" });
    const body = await (await authenticatedRead(deps, "/ui/runs")).text();
    expect(body).toContain("Observation degraded");
    expect(body).toContain("agentctl unavailable");
  });

  test("an old healthy snapshot becomes visibly stale", async () => {
    const { testConfig } = await import("../fakes.ts");
    const deps = buildDeps({ config: privateReadConfig({ agentctl_observer: { enabled: true, observe_all: true } }) });
    deps.store.upsertAgentRun({ executionId: "exec-old", agent: "cursor", state: "running", liveness: "healthy", title: "Old snapshot", updatedAt: "2026-08-20T12:00:00Z" });
    deps.store.kvSet("agentctl.observer.health", { state: "ok", observed_at: "2026-08-20T12:00:00Z" });
    const body = await (await authenticatedRead(deps, "/ui/runs")).text();
    expect(body).toContain("Run status may be stale");
    expect(body).toContain("Last seen Running");
  });

  test("native attention and blocked states enter the attention summary", async () => {
    const now = new Date();
    const store = memoryStore(new FakeClock(now));
    const deps = buildDeps({ store, config: privateReadConfig({ agentctl_observer: { enabled: true, observe_all: true } }) });
    deps.store.upsertAgentRun({ executionId: "exec-attention", agent: "cursor", title: "Permission needed", state: "attention", liveness: "blocked", updatedAt: now.toISOString() });
    deps.store.kvSet("agentctl.observer.health", { state: "ok", observed_at: now.toISOString() });
    const body = await (await authenticatedRead(deps, "/ui/runs?state=attention")).text();
    expect(body).toContain("Permission needed");
    expect(body).toContain("Needs attention");
    expect(body).toContain(">0</span><span class=\"overview-label\">active");
    expect(body).toContain(">1</span><span class=\"overview-label\">need attention");
  });

  test("distinguishes partial terminal history from incomplete active coverage", async () => {
    const now = new Date();
    const store = memoryStore(new FakeClock(now));
    const deps = buildDeps({ store, config: privateReadConfig({ agentctl_observer: { enabled: true, observe_all: true } }) });
    deps.store.kvSet("agentctl.observer.health", { state: "ok", observed_at: now.toISOString(), coverage_degraded: false, history_truncated: true });
    const body = await (await authenticatedRead(deps, "/ui/runs")).text();
    expect(body).toContain("Older history is partial");
    expect(body).toContain("Active coverage is complete");
    expect(body).not.toContain("Active coverage is incomplete");
  });

  test("renders reliable active work with live refresh and useful details", async () => {
    const now = new Date();
    const store = memoryStore(new FakeClock(now));
    const deps = buildDeps({ store, config: privateReadConfig({ agentctl_observer: { enabled: true, required_labels: ["car-observe"] } }) });
    deps.store.upsertAgentRun({
      executionId: "exec-live",
      agent: "cursor",
      authority: "native",
      mode: "direct",
      state: "running",
      liveness: "alive",
      labels: ["car-observe", "runs-live-truth"],
      title: "Runs Live Truth",
      startedAt: new Date(now.getTime() - 65_000).toISOString(),
      updatedAt: now.toISOString(),
      runtime: "cursor/current",
    });
    deps.store.kvSet("agentctl.observer.health", { state: "ok", observed_at: now.toISOString(), run_count: 1, coverage_degraded: false });
    const body = await (await authenticatedRead(deps, "/ui/runs")).text();
    expect(body).toContain("Runs Live Truth");
    expect(body).toContain("Live · refreshed just now");
    expect(body).toContain('data-live-refresh="true"');
    expect(body).toContain(">1</span><span class=\"overview-label\">active");
    expect(body).toContain("Runs labeled car-observe");
    expect(body).toContain("cursor/current");
    expect(body).toContain("Liveness");
  });

  test("filters runs without changing full-set summary counts", async () => {
    const now = new Date();
    const store = memoryStore(new FakeClock(now));
    const deps = buildDeps({ store, config: privateReadConfig({ agentctl_observer: { enabled: true, observe_all: true } }) });
    for (const [id, agent, title] of [["exec-cursor-done", "cursor", "Cursor finished"], ["exec-omp-done", "omp", "OMP finished"]] as const) {
      deps.store.upsertAgentRun({ executionId: id, agent, title, state: "completed", liveness: "exited", updatedAt: now.toISOString(), terminalAt: now.toISOString() });
    }
    deps.store.upsertAgentRun({ executionId: "exec-active", agent: "omp", title: "OMP active", state: "running", liveness: "alive", updatedAt: now.toISOString() });
    deps.store.kvSet("agentctl.observer.health", { state: "ok", observed_at: now.toISOString() });
    const body = await (await authenticatedRead(deps, "/ui/runs?state=finished&agent=cursor")).text();
    expect(body).toContain("Cursor finished");
    expect(body).not.toContain("OMP finished");
    expect(body).not.toContain("OMP active");
    expect(body).toContain("Finished <span class=\"count\">2");
    expect(body).toContain(">1</span><span class=\"overview-label\">active");
  });

  test("pins active work ahead of paginated terminal history", async () => {
    const now = new Date();
    const store = memoryStore(new FakeClock(now));
    const deps = buildDeps({ store, config: privateReadConfig({ agentctl_observer: { enabled: true, observe_all: true } }) });
    deps.store.upsertAgentRun({ executionId: "exec-active", agent: "cursor", title: "Pinned active work", state: "running", liveness: "alive", updatedAt: new Date(now.getTime() - 86_400_000).toISOString() });
    for (let i = 0; i < 51; i++) {
      const at = new Date(now.getTime() - i * 1_000).toISOString();
      deps.store.upsertAgentRun({ executionId: `exec-done-${i}`, agent: "omp", title: `Finished ${i}`, state: "completed", liveness: "exited", updatedAt: at, terminalAt: at });
    }
    deps.store.kvSet("agentctl.observer.health", { state: "ok", observed_at: now.toISOString() });
    const body = await (await authenticatedRead(deps, "/ui/runs")).text();
    expect(body).toContain("Pinned active work");
    expect(body).toContain("Older");
    expect(body).toContain("Finished <span class=\"count\">51");
  });
});
