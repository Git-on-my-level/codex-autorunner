/**
 * File inbox — the universal fallback rung (DESIGN §8). v2's
 * `tickets/replies.py` inbox, generalized and part of the public contract:
 *
 *   ~/.car/replies/<car_session_id>/reply-0001.md
 *
 * Each file carries a small frontmatter block so a consumer (the shipped
 * `UserPromptSubmit` hook snippet) knows when the reply was staged, which
 * channel it was meant for, and which escalation it answers.
 *
 * Consumed replies are archived by the consumer to
 * `<car_session_id>/reply_history/` (documented behavior, WS-G). The sequence
 * number is therefore derived from the directory listing INCLUDING the archive,
 * never from a row count, so archiving can never make two replies collide.
 */
import { mkdirSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { repliesDir } from "../../config/config.ts";
import type { Adapter, AdapterOutcome, DeliveryContext } from "./types.ts";
import { hintString } from "./types.ts";

export const REPLY_FILE_RE = /^reply-(\d{4,})\.md$/;
export const ARCHIVE_DIRNAME = "reply_history";

function listSeqs(dir: string): number[] {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return [];
  }
  const seqs: number[] = [];
  for (const name of entries) {
    const m = REPLY_FILE_RE.exec(name);
    if (m?.[1]) seqs.push(Number(m[1]));
  }
  return seqs;
}

/** Next sequence number, counting both live and archived replies. */
export function nextSeq(dir: string): number {
  const seqs = [...listSeqs(dir), ...listSeqs(join(dir, ARCHIVE_DIRNAME))];
  return seqs.length === 0 ? 1 : Math.max(...seqs) + 1;
}

/** Frontmatter values are emitted as quoted YAML scalars; keep them scalar-safe. */
function yamlSafe(value: string): string {
  return value.replace(/["\\\n\r]/g, "").slice(0, 256);
}

export function replyFileName(seq: number): string {
  return `reply-${String(seq).padStart(4, "0")}.md`;
}

export function renderReplyFile(input: {
  ts: string;
  kind: string;
  escalationId: string;
  text: string;
}): string {
  return [
    "---",
    `ts: "${yamlSafe(input.ts)}"`,
    `kind: "${yamlSafe(input.kind)}"`,
    `escalation_id: "${yamlSafe(input.escalationId)}"`,
    "---",
    "",
    input.text,
    "",
  ].join("\n");
}

export const fileAdapter: Adapter = {
  kind: "file",
  async deliver(ctx: DeliveryContext): Promise<AdapterOutcome> {
    const dir = join(repliesDir(ctx.config), ctx.carSessionId);
    mkdirSync(dir, { recursive: true });
    const seq = nextSeq(dir);
    const name = replyFileName(seq);
    const path = join(dir, name);
    const escalationId = hintString(ctx.hint, "escalation_id", "escalationId") ?? "";
    const body = renderReplyFile({
      ts: ctx.store.clock.now().toISOString(),
      kind: ctx.channel?.kind ?? "file",
      escalationId,
      text: ctx.text,
    });
    await Bun.write(path, body);
    ctx.store.audit("adapter:file", "reply.staged", "session", ctx.carSessionId, {
      path,
      seq,
      kind: ctx.channel?.kind ?? "file",
      escalation_id: escalationId || null,
    });
    return { status: "delivered", detail: { path, seq } };
  },
};
