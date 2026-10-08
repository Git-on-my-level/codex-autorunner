import { afterEach, describe, expect, test } from "bun:test";
import { buildPayload, createDeadmanLoop, DEADMAN_CONTRACT } from "../../src/ops/deadman.ts";
import { FakeClock, memoryStore, testConfig } from "../fakes.ts";

const TOKEN_ENV = "CAR_TEST_DEADMAN_TOKEN";

afterEach(() => {
  delete process.env[TOKEN_ENV];
});

describe("external dead-man heartbeat", () => {
  test("reports only a canonical digest receipt, never enqueue time", () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const first = buildPayload(store, "daemon-test", 1, clock.current.toISOString());
    expect(first.last_digest_receipt_at).toBeNull();

    store.recordDigest("2026-08-26", "☀️ digest", []);
    const outbox = store.enqueueOutboxIntent({
      intentId: "scheduled-digest-test",
      channel: "telegram",
      target: { kind: "digest" },
      body: { text: "☀️ digest" },
    });
    store.attachDigestOutbox("2026-08-26", outbox.outboxId);
    const [claimed] = store.claimPendingOutbox(1, "telegram-outbox", 120);
    expect(claimed).toBeDefined();

    const beforeReceipt = buildPayload(store, "daemon-test", 2, clock.current.toISOString());
    expect(beforeReceipt.last_digest_receipt_at).toBeNull();

    store.recordOutboxReceipt(
      outbox.outboxId,
      { owner: "telegram-outbox", token: claimed!.claim_token! },
      "delivered",
      { sentMessageId: "1001" },
    );
    const afterReceipt = buildPayload(store, "daemon-test", 3, clock.current.toISOString());
    expect(afterReceipt.last_digest_receipt_at).toBe(clock.current.toISOString());
  });

  test("sends authenticated monotonic evidence without exposing the token", async () => {
    process.env[TOKEN_ENV] = "secret-heartbeat-token";
    const clock = new FakeClock();
    const store = memoryStore(clock);
    store.audit("daemon", "event.ingested", "event", "evt_1", {});
    const calls: { url: string; init: RequestInit }[] = [];
    const fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), init: init ?? {} });
      return new Response("", { status: 204 });
    }) as typeof globalThis.fetch;
    const config = testConfig({
      deadman: {
        enabled: true,
        url: "https://observer.example/heartbeat",
        token_env: TOKEN_ENV,
        interval_seconds: 60,
      },
    });

    const loop = createDeadmanLoop(store, config, { fetch, instanceId: "daemon-test" });
    await loop.start();
    await loop.stop();

    expect(calls).toHaveLength(1);
    expect(calls[0]!.init.headers).toMatchObject({ authorization: "Bearer secret-heartbeat-token" });
    const payload = JSON.parse(String(calls[0]!.init.body));
    expect(payload).toMatchObject({ contract: DEADMAN_CONTRACT, daemon_instance_id: "daemon-test", seq: 1 });
    expect(JSON.stringify(payload)).not.toContain("secret-heartbeat-token");
    expect(store.kvGet<string>("deadman:last_status")).toBe("healthy");
  });

  test("fails visibly when credentials are missing and does not throw the daemon loop", async () => {
    const store = memoryStore();
    const config = testConfig({
      deadman: {
        enabled: true,
        url: "https://observer.example/heartbeat",
        token_env: TOKEN_ENV,
        interval_seconds: 60,
      },
    });
    const loop = createDeadmanLoop(store, config, { instanceId: "daemon-test" });
    await loop.start();
    await loop.stop();
    expect(store.kvGet<string>("deadman:last_status")).toBe("failed");
    const row = store.db.query("SELECT detail_json FROM audit WHERE verb = 'deadman.failed'").get() as {
      detail_json: string;
    };
    expect(row.detail_json).toContain(`missing $${TOKEN_ENV}`);
  });
});
