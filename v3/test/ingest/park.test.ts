/**
 * The parked PermissionRequest path.
 *
 * POST /v1/ingest/claude holds the hook's HTTP response open while triage or a
 * Telegram tap races the deadline. A decision is answered in-band; a timeout
 * returns an empty 200 so Claude falls back to its local prompt.
 */
import { afterEach, describe, expect, test } from "bun:test";
import { fixtureInput, harness } from "./harness.ts";
import { answerPermission, hasParkedPermission, releaseAllParks } from "../../src/permission_park.ts";
import { hookDecisionBody, parkDeadlineMs, readHookTimeoutHint } from "../../src/ingest/claude.ts";

afterEach(() => releaseAllParks());

const KEY = "claude-code:sess-abc123:PermissionRequest:toolu_01X";

/** Fire the hook request and hand back both the pending response and the event id. */
async function parkRequest(h: ReturnType<typeof harness>, payload: unknown = fixtureInput("claude", "06-")) {
  const pending = h.post("/v1/ingest/claude", payload);
  const eventId = await h.waitForEventId(
    typeof payload === "object" && payload !== null && "session_id" in payload
      ? `claude-code:${(payload as { session_id: string }).session_id}:PermissionRequest:${
          (payload as { tool_use_id?: string }).tool_use_id ?? ""
        }`
      : KEY,
  );
  return { pending, eventId };
}

describe("PermissionRequest park", () => {
  test("an allow decision comes back as the hook-decision JSON Claude expects", async () => {
    const h = harness();
    const { pending, eventId } = await parkRequest(h);

    expect(hasParkedPermission(eventId)).toBe(true);
    expect(answerPermission(eventId, { decision: "allow", reason: "memory: always allow gh pr merge" })).toBe(
      true,
    );

    const res = await pending;
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      hookSpecificOutput: {
        hookEventName: "PermissionRequest",
        permissionDecision: "allow",
        permissionDecisionReason: "memory: always allow gh pr merge",
      },
    });
  });

  test("a deny decision carries the required reason", async () => {
    const h = harness();
    const { pending, eventId } = await parkRequest(h);
    answerPermission(eventId, { decision: "deny", reason: "you have denied force-push on this repo twice" });

    expect(await (await pending).json()).toEqual({
      hookSpecificOutput: {
        hookEventName: "PermissionRequest",
        permissionDecision: "deny",
        permissionDecisionReason: "you have denied force-push on this repo twice",
      },
    });
  });

  test("a decision without a reason still emits permissionDecisionReason", async () => {
    const h = harness();
    const { pending, eventId } = await parkRequest(h);
    answerPermission(eventId, { decision: "deny" });
    expect(await (await pending).json()).toMatchObject({
      hookSpecificOutput: { permissionDecisionReason: "Denied via CAR" },
    });
  });

  test("a timeout returns an empty 200 so Claude falls back to its local prompt", async () => {
    // 300ms hook timeout -> the 5s margin clamps the park to the 250ms floor.
    const h = harness({}, { defaultHookTimeoutMs: 300 });
    const res = await h.post("/v1/ingest/claude", fixtureInput("claude", "06-"));

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({});
  });

  test("the event is durable even when the park times out", async () => {
    const h = harness({}, { defaultHookTimeoutMs: 300 });
    await h.post("/v1/ingest/claude", fixtureInput("claude", "06-"));

    const rows = h.events();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ idempotency_key: KEY, type: "attention.permission" });
  });

  test("park and outcome are both audited", async () => {
    const h = harness({}, { defaultHookTimeoutMs: 300 });
    await h.post("/v1/ingest/claude", fixtureInput("claude", "06-"));
    expect(h.audits("permission.parked")).toHaveLength(1);
    expect(h.audits("permission.timed_out")).toHaveLength(1);

    const h2 = harness();
    const { pending, eventId } = await parkRequest(h2);
    answerPermission(eventId, { decision: "allow" });
    await pending;
    expect(h2.audits("permission.answered")).toHaveLength(1);
    expect(JSON.parse(h2.audits("permission.answered")[0]!.detail_json)).toEqual({ decision: "allow" });
  });

  test("a redelivery while a park is in flight degrades instead of orphaning it", async () => {
    const h = harness();
    const { pending, eventId } = await parkRequest(h);

    // Same idempotency key: the store returns the existing event id, and parking
    // again would overwrite the registry entry and strand the first response.
    const redelivery = await h.post("/v1/ingest/claude", fixtureInput("claude", "06-"));
    expect(await redelivery.json()).toEqual({});

    // The original park is untouched and still answerable.
    expect(hasParkedPermission(eventId)).toBe(true);
    answerPermission(eventId, { decision: "allow" });
    expect(await (await pending).json()).toMatchObject({
      hookSpecificOutput: { permissionDecision: "allow" },
    });
    expect(h.events()).toHaveLength(1);
  });

  test("shutdown releases parks so held responses close", async () => {
    const h = harness();
    const { pending } = await parkRequest(h);
    releaseAllParks();
    expect(await (await pending).json()).toEqual({});
  });

  test("non-decision hooks are never parked", async () => {
    const h = harness();
    const res = await h.post("/v1/ingest/claude", fixtureInput("claude", "03-"));
    expect(res.status).toBe(200);
    expect((await res.json()) as Record<string, unknown>).toMatchObject({ ok: true, inserted: true });
  });

  test("the recorded deadline_ms equals the deadline actually parked for", async () => {
    const h = harness();
    const { pending, eventId } = await parkRequest(h);

    const row = h.store.db
      .query("SELECT response_channel_json FROM events WHERE id = ?")
      .get(eventId) as { response_channel_json: string };
    const channel = JSON.parse(row.response_channel_json);
    expect(channel.kind).toBe("claude-hook-http");
    // 60s documented default minus the 5s margin.
    expect(channel.hint.deadline_ms).toBe(55_000);

    const parked = JSON.parse(h.audits("permission.parked")[0]!.detail_json);
    expect(parked.deadline_ms).toBe(channel.hint.deadline_ms);
    expect(parked.tool_use_id).toBe("toolu_01X");

    answerPermission(eventId, { decision: "allow" });
    await pending;
  });

  test("a header timeout hint overrides the default for both hint and park", async () => {
    const h = harness();
    const pending = h.post("/v1/ingest/claude", fixtureInput("claude", "06-"), {
      headers: { "x-car-hook-timeout-ms": "20000" },
    });
    const eventId = await h.waitForEventId(KEY);

    const row = h.store.db
      .query("SELECT response_channel_json FROM events WHERE id = ?")
      .get(eventId) as { response_channel_json: string };
    expect(JSON.parse(row.response_channel_json).hint.deadline_ms).toBe(15_000);

    answerPermission(eventId, { decision: "allow" });
    await pending;
  });
});

describe("hook timeout hint resolution", () => {
  test("query params beat headers beat payload", () => {
    const url = new URL("http://x/v1/ingest/claude?timeout_ms=1000");
    const headers = new Headers({ "x-car-hook-timeout-ms": "2000" });
    expect(readHookTimeoutHint({ timeout_ms: 3000 }, headers, url)).toBe(1000);
    expect(readHookTimeoutHint({ timeout_ms: 3000 }, headers)).toBe(2000);
    expect(readHookTimeoutHint({ timeout_ms: 3000 })).toBe(3000);
  });

  test("seconds-valued hints are converted to milliseconds", () => {
    expect(readHookTimeoutHint({ timeout: 30 })).toBe(30_000);
    expect(readHookTimeoutHint({ hook_timeout: "45" })).toBe(45_000);
  });

  test("no hint at all is undefined, so callers can layer their own default", () => {
    expect(readHookTimeoutHint({})).toBeUndefined();
  });

  test("the deadline is the hint minus the margin, clamped", () => {
    expect(parkDeadlineMs(60_000)).toBe(55_000);
    expect(parkDeadlineMs(300)).toBe(250); // floor
    expect(parkDeadlineMs(10_000_000)).toBe(600_000); // ceiling
  });
});

describe("hookDecisionBody", () => {
  test("maps decisions onto Claude's hookSpecificOutput shape", () => {
    expect(hookDecisionBody({ decision: "allow" })).toEqual({
      hookSpecificOutput: {
        hookEventName: "PermissionRequest",
        permissionDecision: "allow",
        permissionDecisionReason: "Approved via CAR",
      },
    });
  });
});
