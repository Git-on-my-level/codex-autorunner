/**
 * Reply-back dispatch: one test per ResponseChannelKind, plus the fallback
 * chains that make "a dropped reply" impossible.
 */
import { afterEach, describe, expect, test } from "bun:test";
import { readdirSync, rmSync, writeFileSync } from "node:fs";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createActionBus } from "../../src/actions/index.ts";
import { parkPermission, releaseAllParks } from "../../src/permission_park.ts";
import { CarConfig } from "../../src/config/config.ts";
import { repliesDir } from "../../src/config/config.ts";
import { CarEvent } from "../../src/contract/events.ts";
import type { Store } from "../../src/store/db.ts";
import { FakeClock, memoryStore } from "../fakes.ts";
import {
  auditRows,
  auditVerbs,
  helpResponder,
  makeHttp,
  makeRunner,
  seedSession,
  StubPolicy,
  tempState,
} from "./helpers.ts";

const cleanups: (() => void)[] = [];
afterEach(() => {
  releaseAllParks();
  while (cleanups.length) cleanups.pop()!();
});

function harness(runnerRespond?: Parameters<typeof makeRunner>[0], http = makeHttp()) {
  const state = tempState();
  cleanups.push(state.cleanup);
  const clock = new FakeClock();
  const store = memoryStore(clock);
  const policy = new StubPolicy();
  const runner = makeRunner(helpResponder(runnerRespond ?? (() => undefined)));
  const bus = createActionBus(store, state.config, policy, {
    runner: runner.runner,
    httpFetch: http.fetch,
    env: { CAR_MULTICA_URL: "https://multica.test", CAR_MULTICA_TOKEN: "tok" },
  });
  return { state, store, clock, policy, runner, http, bus };
}

/** Ingest a permission event the way WS-A's claude hook route would. */
function ingestPermission(store: Store, idempotencyKey: string, ts: string): string {
  return store.ingestEvent(
    CarEvent.parse({
      contract: "car.event.v1",
      idempotency_key: idempotencyKey,
      ts,
      source: { vendor: "claude-code", host: "test-host", adapter: "hook-http" },
      session: { vendor: "claude-code", native_id: "sess-abc", host: "test-host" },
      type: "attention.permission",
      severity: "attention",
      requires_response: true,
      response_channel: { kind: "claude-hook-http", hint: { tool_use_id: "toolu_01X" } },
      title: "Permission: gh pr merge 42",
    }),
  ).event_id;
}

function replyFiles(config: CarConfig, carSessionId: string): string[] {
  try {
    return readdirSync(join(repliesDir(config), carSessionId)).sort();
  } catch {
    return [];
  }
}

describe("deliver — claude-hook-http", () => {
  test("answers the parked permission in-band", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "claude-code", native_id: "sess-abc" });
    const parked = parkPermission("evt_1", 10_000);

    const result = await h.bus.deliver(
      sid,
      { kind: "claude-hook-http", hint: { event_id: "evt_1" } },
      { approval: true },
    );

    expect(result).toBe("delivered");
    expect(await parked).toEqual({ decision: "allow" });
    expect(h.runner.calls).toHaveLength(0);
    expect(replyFiles(h.state.config, sid)).toHaveLength(0);
    expect(auditVerbs(h.store, sid)).toContain("reply.delivered");
  });

  test("deny is carried through as a deny decision", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "claude-code", native_id: "sess-abc" });
    const parked = parkPermission("evt_2", 10_000);
    const result = await h.bus.deliver(
      sid,
      { kind: "claude-hook-http", hint: { event_id: "evt_2" } },
      { approval: false, text: "you denied force-push here twice" },
    );
    expect(result).toBe("delivered");
    expect(await parked).toEqual({ decision: "deny", reason: "you denied force-push here twice" });
  });

  test("park already timed out → claude-resume rung reaches the agent", async () => {
    const h = harness();
    const sid = seedSession(h.store, {
      vendor: "claude-code",
      native_id: "sess-abc",
      cwd: "/Users/dazheng/omi",
    });
    // No park exists for this event id: answerPermission returns false.
    const result = await h.bus.deliver(
      sid,
      { kind: "claude-hook-http", hint: { event_id: "evt_missing" } },
      { approval: true },
    );
    expect(result).toBe("delivered");
    expect(h.runner.lines()).toContain("claude -p --resume sess-abc APPROVED");
    const resume = h.runner.calls.find((c) => c.argv[1] === "-p");
    expect(resume?.opts.cwd).toBe("/Users/dazheng/omi");
    expect(replyFiles(h.state.config, sid)).toHaveLength(0);
  });

  test("park timed out AND resume fails → file inbox, degraded", async () => {
    const h = harness((argv) =>
      argv[1] === "-p" ? { code: 1, stderr: "no conversation found" } : undefined,
    );
    const sid = seedSession(h.store, { vendor: "claude-code", native_id: "sess-abc" });

    const result = await h.bus.deliver(
      sid,
      { kind: "claude-hook-http", hint: { event_id: "evt_gone", escalation_id: "esc_9" } },
      { approval: true },
    );

    expect(result).toBe("degraded");
    const files = replyFiles(h.state.config, sid);
    expect(files).toEqual(["reply-0001.md"]);
    const verbs = auditVerbs(h.store, sid);
    // Three rungs attempted, ending in a staged file.
    expect(verbs.filter((v) => v === "reply.attempt")).toHaveLength(3);
    expect(verbs).toContain("reply.staged");
    expect(verbs).toContain("reply.degraded");
  });

  test("without an event_id hint, the still-parked permission event is found", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "claude-code", native_id: "sess-abc" });
    // Two permission events for the session; only the newer one is parked.
    const stale = ingestPermission(h.store, "k1", "2026-08-26T11:00:00.000Z");
    h.clock.advance(60_000);
    const live = ingestPermission(h.store, "k2", "2026-08-26T11:59:00.000Z");
    expect(stale).not.toBe(live);
    const parked = parkPermission(live, 10_000);

    const result = await h.bus.deliver(sid, { kind: "claude-hook-http" }, { approval: true });

    expect(result).toBe("delivered");
    expect(await parked).toEqual({ decision: "allow" });
  });

  test("no parked request at all → falls through to resume", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "claude-code", native_id: "sess-abc" });
    const result = await h.bus.deliver(sid, { kind: "claude-hook-http" }, { approval: true });
    expect(result).toBe("delivered");
    expect(h.runner.lines()).toContain("claude -p --resume sess-abc APPROVED");
  });

  test("a prose reply cannot answer a permission park, so it falls through", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "claude-code", native_id: "sess-abc" });
    parkPermission("evt_3", 10_000);
    const result = await h.bus.deliver(
      sid,
      { kind: "claude-hook-http", hint: { event_id: "evt_3" } },
      { text: "rebase instead" },
    );
    expect(result).toBe("delivered");
    expect(h.runner.lines()).toContain("claude -p --resume sess-abc rebase instead");
  });
});

describe("deliver — claude-resume", () => {
  test("resumes in the session cwd", async () => {
    const h = harness();
    const sid = seedSession(h.store, {
      vendor: "claude-code",
      native_id: "sess-xyz",
      cwd: "/repo/omi",
    });
    const result = await h.bus.deliver(sid, { kind: "claude-resume" }, { text: "keep going" });
    expect(result).toBe("delivered");
    expect(h.runner.lines()).toContain("claude -p --resume sess-xyz keep going");
    expect(h.runner.calls.at(-1)?.opts.cwd).toBe("/repo/omi");
  });

  test("no claude ref → file fallback", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "codex", native_id: "uuid-1" });
    const result = await h.bus.deliver(sid, { kind: "claude-resume" }, { text: "hi" });
    expect(result).toBe("degraded");
    expect(replyFiles(h.state.config, sid)).toEqual(["reply-0001.md"]);
  });
});

describe("deliver — codex-exec-resume", () => {
  test("codex exec resume <uuid> <text>", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "codex", native_id: "uuid-1", cwd: "/repo/x" });
    const result = await h.bus.deliver(sid, { kind: "codex-exec-resume" }, { text: "ship it" });
    expect(result).toBe("delivered");
    expect(h.runner.lines()).toContain("codex exec resume uuid-1 ship it");
  });

  test("nonzero exit → file fallback, degraded", async () => {
    const h = harness((argv) => (argv[1] === "exec" ? { code: 2, stderr: "session gone" } : undefined));
    const sid = seedSession(h.store, { vendor: "codex", native_id: "uuid-1" });
    const result = await h.bus.deliver(sid, { kind: "codex-exec-resume" }, { text: "ship it" });
    expect(result).toBe("degraded");
    expect(replyFiles(h.state.config, sid)).toEqual(["reply-0001.md"]);
  });
});

describe("deliver — agentctl-run", () => {
  test("launches a labelled continuation and subscribes for the new exec", async () => {
    const h = harness((argv) =>
      argv[1] === "run"
        ? { code: 0, stdout: JSON.stringify({ ok: true, result: { execution_id: "exec-77" } }) }
        : undefined,
    );
    const sid = seedSession(h.store, { vendor: "codex", native_id: "uuid-9", cwd: "/repo/y" });

    const result = await h.bus.deliver(sid, { kind: "agentctl-run" }, { text: "continue please" });

    expect(result).toBe("delivered");
    expect(h.runner.lines()).toContain(
      "agentctl run --background --label car-continuation -- codex exec resume uuid-9 continue please",
    );
    expect(h.runner.lines()).toContain(
      `agentctl subscribe create --execution exec-77 --destination webhook --target http://127.0.0.1:${h.state.config.http.port}/v1/ingest/agentctl`,
    );
    expect(auditVerbs(h.store, sid)).toContain("agentctl.subscribed");
    // The continuation is linked as another ref of the same CAR session.
    const refs = h.store.db
      .query("SELECT native_id FROM session_refs WHERE car_session_id = ? ORDER BY native_id")
      .all(sid) as { native_id: string }[];
    expect(refs.map((r) => r.native_id)).toEqual(["exec-77", "uuid-9"]);
  });

  test("builds a claude resume argv when the underlying ref is claude", async () => {
    const h = harness((argv) => (argv[1] === "run" ? { code: 0, stdout: "started exec-12" } : undefined));
    const sid = seedSession(h.store, { vendor: "claude-code", native_id: "sess-k" });
    const result = await h.bus.deliver(sid, { kind: "agentctl-run" }, { approval: true });
    expect(result).toBe("delivered");
    expect(h.runner.lines()).toContain(
      "agentctl run --background --label car-continuation -- claude -p --resume sess-k APPROVED",
    );
  });

  test("subscribe failure is audited but the launch still counts as delivered", async () => {
    const h = harness((argv) => {
      if (argv[1] === "run") return { code: 0, stdout: "exec-55" };
      if (argv[1] === "subscribe") return { code: 3, stderr: "no such execution" };
      return undefined;
    });
    const sid = seedSession(h.store, { vendor: "codex", native_id: "uuid-2" });
    const result = await h.bus.deliver(sid, { kind: "agentctl-run" }, { text: "go" });
    expect(result).toBe("delivered");
    expect(auditVerbs(h.store, sid)).toContain("agentctl.subscribe_failed");
  });

  test("launch failure → file fallback", async () => {
    const h = harness((argv) => (argv[1] === "run" ? { code: 1, stderr: "boom" } : undefined));
    const sid = seedSession(h.store, { vendor: "codex", native_id: "uuid-3" });
    const result = await h.bus.deliver(sid, { kind: "agentctl-run" }, { text: "go" });
    expect(result).toBe("degraded");
    expect(replyFiles(h.state.config, sid)).toEqual(["reply-0001.md"]);
  });
});

describe("deliver — multica-api", () => {
  test("posts a comment for a text reply", async () => {
    const http = makeHttp();
    const h = harness(undefined, http);
    const sid = seedSession(h.store, { vendor: "multica", native_id: "card-3" });
    const result = await h.bus.deliver(
      sid,
      { kind: "multica-api", hint: { card_id: "card-3" } },
      { text: "looks good" },
    );
    expect(result).toBe("delivered");
    expect(http.calls[0]?.url).toBe("https://multica.test/api/v1/cards/card-3/comments");
    expect(JSON.parse(http.calls[0]?.init?.body ?? "{}")).toEqual({ body: "looks good" });
    expect(http.calls[0]?.init?.headers?.["authorization"]).toBe("Bearer tok");
  });

  test("posts an approval for an approval reply", async () => {
    const http = makeHttp();
    const h = harness(undefined, http);
    const sid = seedSession(h.store, { vendor: "multica", native_id: "card-4" });
    const result = await h.bus.deliver(
      sid,
      { kind: "multica-api", hint: { card_id: "card-4" } },
      { approval: false },
    );
    expect(result).toBe("delivered");
    expect(http.calls[0]?.url).toBe("https://multica.test/api/v1/cards/card-4/approvals");
    expect(JSON.parse(http.calls[0]?.init?.body ?? "{}")).toEqual({ approved: false, comment: "DENIED" });
  });

  test("non-2xx → file fallback", async () => {
    const http = makeHttp(() => ({ status: 502, body: "bad gateway" }));
    const h = harness(undefined, http);
    const sid = seedSession(h.store, { vendor: "multica", native_id: "card-5" });
    const result = await h.bus.deliver(
      sid,
      { kind: "multica-api", hint: { card_id: "card-5" } },
      { text: "hi" },
    );
    expect(result).toBe("degraded");
    expect(replyFiles(h.state.config, sid)).toEqual(["reply-0001.md"]);
  });

  test("missing base URL → file fallback, no HTTP attempted", async () => {
    const state = tempState();
    cleanups.push(state.cleanup);
    const store = memoryStore(new FakeClock());
    const http = makeHttp();
    const bus = createActionBus(store, state.config, new StubPolicy(), {
      runner: makeRunner().runner,
      httpFetch: http.fetch,
      env: {},
    });
    const sid = seedSession(store, { vendor: "multica", native_id: "card-6" });
    const result = await bus.deliver(
      sid,
      { kind: "multica-api", hint: { card_id: "card-6" } },
      { text: "hi" },
    );
    expect(result).toBe("degraded");
    expect(http.calls).toHaveLength(0);
  });

  test("a hint path may not escape the configured base host", async () => {
    const http = makeHttp();
    const h = harness(undefined, http);
    const sid = seedSession(h.store, { vendor: "multica", native_id: "card-7" });
    const result = await h.bus.deliver(
      sid,
      { kind: "multica-api", hint: { card_id: "card-7", path: "https://evil.test/steal" } },
      { text: "hi" },
    );
    expect(result).toBe("degraded");
    expect(http.calls).toHaveLength(0);
  });
});

describe("deliver — file and total failure", () => {
  test("a null channel is queued, not degraded", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "cron", native_id: "job-1" });
    const result = await h.bus.deliver(sid, null, { text: "note" });
    expect(result).toBe("queued");
    expect(auditVerbs(h.store, sid)).toContain("reply.queued");
  });

  test("kind 'file' is queued", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "other", native_id: "x" });
    expect(await h.bus.deliver(sid, { kind: "file" }, { text: "note" })).toBe("queued");
  });

  test("every rung failing returns 'failed' and audits reply.delivery_failed", async () => {
    // state_dir is a regular file, so the replies directory cannot be created.
    const dir = mkdtempSync(join(tmpdir(), "car-ws-e-bad-"));
    const blocker = join(dir, "not-a-dir");
    writeFileSync(blocker, "x");
    cleanups.push(() => rmSync(dir, { recursive: true, force: true }));

    const store = memoryStore(new FakeClock());
    const config = CarConfig.parse({ state_dir: blocker });
    const runner = makeRunner(helpResponder((argv) => (argv[1] === "exec" ? { code: 1 } : undefined)));
    const bus = createActionBus(store, config, new StubPolicy(), { runner: runner.runner, env: {} });
    const sid = seedSession(store, { vendor: "codex", native_id: "uuid-x" });

    const result = await bus.deliver(sid, { kind: "codex-exec-resume" }, { text: "hi" });

    expect(result).toBe("failed");
    const failures = auditRows(store, "reply.delivery_failed");
    expect(failures).toHaveLength(1);
    expect(failures[0]?.object_id).toBe(sid);
    expect((failures[0]?.detail as { attempts: unknown[] }).attempts).toHaveLength(2);
  });
});
