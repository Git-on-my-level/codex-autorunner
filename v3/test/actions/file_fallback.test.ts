/**
 * The universal fallback rung: frontmatter, sequential names, and a sequence
 * that survives archiving (consumed replies move to reply_history/).
 */
import { afterEach, describe, expect, test } from "bun:test";
import { mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createActionBus } from "../../src/actions/index.ts";
import { nextSeq, renderReplyFile, replyFileName } from "../../src/actions/adapters/file.ts";
import { repliesDir } from "../../src/config/config.ts";
import { FakeClock, memoryStore } from "../fakes.ts";
import { makeRunner, seedSession, StubPolicy, tempState } from "./helpers.ts";

const cleanups: (() => void)[] = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

function harness() {
  const state = tempState();
  cleanups.push(state.cleanup);
  const store = memoryStore(new FakeClock());
  const bus = createActionBus(store, state.config, new StubPolicy(), {
    runner: makeRunner().runner,
    env: {},
  });
  return { state, store, bus };
}

describe("file inbox", () => {
  test("writes frontmatter with ts, kind and escalation id", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "other", native_id: "s1" });
    await h.bus.deliver(sid, { kind: "file", hint: { escalation_id: "esc_42" } }, { text: "rebase instead" });

    const path = join(repliesDir(h.state.config), sid, "reply-0001.md");
    const text = await Bun.file(path).text();
    expect(text).toBe(
      [
        "---",
        'ts: "2026-08-26T12:00:00.000Z"',
        'kind: "file"',
        'escalation_id: "esc_42"',
        "---",
        "",
        "rebase instead",
        "",
      ].join("\n"),
    );
  });

  test("records the originating channel kind, not 'file'", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "codex", native_id: "s2" });
    // No codex ref resume possible: the fake runner reports codex has no resume.
    await h.bus.deliver(sid, { kind: "multica-api", hint: {} }, { approval: true });
    const text = await Bun.file(join(repliesDir(h.state.config), sid, "reply-0001.md")).text();
    expect(text).toContain('kind: "multica-api"');
    expect(text).toContain("APPROVED");
  });

  test("names are sequential across replies", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "other", native_id: "s3" });
    for (let i = 0; i < 3; i++) {
      await h.bus.deliver(sid, { kind: "file" }, { text: `reply ${i}` });
    }
    expect(readdirSync(join(repliesDir(h.state.config), sid)).sort()).toEqual([
      "reply-0001.md",
      "reply-0002.md",
      "reply-0003.md",
    ]);
  });

  test("sequence comes from the directory listing, not a row count", async () => {
    const h = harness();
    const sid = seedSession(h.store, { vendor: "other", native_id: "s4" });
    const dir = join(repliesDir(h.state.config), sid);
    mkdirSync(dir, { recursive: true });
    // A pre-existing reply staged by an earlier daemon lifetime.
    writeFileSync(join(dir, "reply-0007.md"), "old");
    await h.bus.deliver(sid, { kind: "file" }, { text: "new" });
    expect(readdirSync(dir).sort()).toEqual(["reply-0007.md", "reply-0008.md"]);
  });

  test("archived replies still hold their sequence numbers", () => {
    const state = tempState();
    cleanups.push(state.cleanup);
    const dir = join(state.dir, "seq-test");
    mkdirSync(join(dir, "reply_history"), { recursive: true });
    writeFileSync(join(dir, "reply_history", "reply-0004.md"), "consumed");
    writeFileSync(join(dir, "reply-0002.md"), "live");
    expect(nextSeq(dir)).toBe(5);
  });

  test("empty directory starts at 1", () => {
    const state = tempState();
    cleanups.push(state.cleanup);
    expect(nextSeq(join(state.dir, "nothing-here"))).toBe(1);
  });

  test("frontmatter values cannot break out of the YAML scalar", () => {
    const rendered = renderReplyFile({
      ts: "2026-08-26T12:00:00.000Z",
      kind: 'file"\nevil: true',
      escalationId: "esc_1",
      text: "body",
    });
    expect(rendered.split("\n")[2]).toBe('kind: "fileevil: true"');
  });

  test("reply file names are zero padded to four digits", () => {
    expect(replyFileName(1)).toBe("reply-0001.md");
    expect(replyFileName(12345)).toBe("reply-12345.md");
  });
});
