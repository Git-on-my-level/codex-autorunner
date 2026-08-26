/** Dedupe identity: incident lineage keys and action runaway hashes. */
import { describe, expect, test } from "bun:test";
import { actionDedupeHash, dedupeClassFor, stableStringify } from "../../src/triage/dedupe.ts";

const ev = (idempotency_key: string, type = "attention.permission", title = "Permission: gh pr merge 42") => ({
  idempotency_key,
  type,
  title,
});

describe("dedupeClassFor", () => {
  test("strips the per-occurrence segment from a source-scoped key", () => {
    expect(dedupeClassFor(ev("claude-code:sess-abc:PermissionRequest:toolu_01X"))).toBe(
      "claude-code:sess-abc:PermissionRequest",
    );
  });

  test("two occurrences of the same problem share a class", () => {
    const a = dedupeClassFor(ev("claude-code:sess-abc:PermissionRequest:toolu_01X"));
    const b = dedupeClassFor(ev("claude-code:sess-abc:PermissionRequest:toolu_02Y"));
    expect(a).toBe(b);
  });

  test("different hook types do not collide", () => {
    expect(dedupeClassFor(ev("claude-code:sess-abc:PermissionRequest:1"))).not.toBe(
      dedupeClassFor(ev("claude-code:sess-abc:Notification:1")),
    );
  });

  test("different sessions do not collide", () => {
    expect(dedupeClassFor(ev("claude-code:sess-abc:Stop:1"))).not.toBe(
      dedupeClassFor(ev("claude-code:sess-def:Stop:1")),
    );
  });

  test("server-computed keys collapse to vendor + type", () => {
    expect(dedupeClassFor(ev("computed:ci:attention.error:deadbeefdeadbeef", "attention.error"))).toBe(
      "computed:ci:attention.error",
    );
    expect(dedupeClassFor(ev("computed:ci:attention.error:cafebabecafebabe", "attention.error"))).toBe(
      "computed:ci:attention.error",
    );
  });

  test("an unstructured key falls back to a type + title hash", () => {
    const a = dedupeClassFor(ev("opaque-key-1", "attention.error", "disk full"));
    const b = dedupeClassFor(ev("opaque-key-2", "attention.error", "disk full"));
    const c = dedupeClassFor(ev("opaque-key-3", "attention.error", "network down"));
    expect(a).toBe(b);
    expect(a).not.toBe(c);
    expect(a.startsWith("attention.error:")).toBe(true);
  });
});

describe("actionDedupeHash", () => {
  test("argument key order does not change the hash", () => {
    expect(actionDedupeHash("reply", { a: 1, b: 2 })).toBe(actionDedupeHash("reply", { b: 2, a: 1 }));
  });

  test("different args produce different hashes", () => {
    expect(actionDedupeHash("reply", { text: "rebase" })).not.toBe(actionDedupeHash("reply", { text: "merge" }));
  });

  test("the class is part of the identity", () => {
    expect(actionDedupeHash("reply", { x: 1 })).not.toBe(actionDedupeHash("probe", { x: 1 }));
  });

  test("nested objects and arrays are handled deterministically", () => {
    expect(actionDedupeHash("exec", { a: { z: 1, y: [1, 2] } })).toBe(
      actionDedupeHash("exec", { a: { y: [1, 2], z: 1 } }),
    );
    expect(actionDedupeHash("exec", { a: [1, 2] })).not.toBe(actionDedupeHash("exec", { a: [2, 1] }));
  });

  test("stableStringify survives null, undefined and primitives", () => {
    expect(stableStringify(null)).toBe("null");
    expect(stableStringify({ a: undefined, b: 1 })).toBe('{"b":1}');
    expect(stableStringify("x")).toBe('"x"');
    expect(stableStringify(3)).toBe("3");
  });
});
