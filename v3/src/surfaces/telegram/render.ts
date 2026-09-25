/**
 * Pure message rendering + callback-data codec. No grammY, no store, no clock.
 * Everything here is a total function so the UX is testable without a bot.
 */
import type { EscalationMessage } from "../../ports.ts";
import type { InlineKeyboardSpec, MessageSpec } from "./types.ts";

/* --------------------------------------------------------- callback codec */

/**
 * Callback data is `<op>:<id>`. Telegram caps callback_data at 64 bytes; our
 * ids are `esc_`/`dec_`/`mem_` + a 26-char ULID (30 bytes), so every op fits.
 */
export const CB = {
  approve: "ap",
  deny: "dn",
  reply: "rp",
  snoozeMenu: "sz",
  snooze1h: "sz1",
  snoozeTonight: "szn",
  snoozeDigest: "szd",
  backToMain: "bk",
  alwaysMenu: "al",
  alwaysAll: "ala",
  alwaysRepo: "alr",
  alwaysKeep: "alk",
  digestUp: "du",
  digestDown: "dd",
  promoteYes: "pmy",
  promoteRepo: "pmr",
  promoteKeep: "pmk",
  probe: "pb",
  escalateNow: "es",
  memoryReview: "mr",
} as const;

export type CallbackOp = (typeof CB)[keyof typeof CB];

const OPS = new Set<string>(Object.values(CB));

export function encodeCallback(op: CallbackOp, id: string): string {
  return `${op}:${id}`;
}

export function decodeCallback(data: string): { op: CallbackOp; id: string } | null {
  const idx = data.indexOf(":");
  if (idx <= 0) return null;
  const op = data.slice(0, idx);
  const id = data.slice(idx + 1);
  if (!OPS.has(op) || id.length === 0) return null;
  return { op: op as CallbackOp, id };
}

/* ------------------------------------------------------------- escalation */

const SEVERITY_MARK: Record<string, string> = {
  urgent: "🚨",
  attention: "🔴",
  notice: "🟡",
  info: "⚪",
};

export function severityMark(severity: string): string {
  return SEVERITY_MARK[severity] ?? "🔴";
}

export interface EscalationRenderInput extends EscalationMessage {
  /** e.g. "claude-code · omi-desktop @ mac-studio". Omitted for sessionless events. */
  sessionLabel?: string;
  /** Free-text questions have no approve/deny semantics; default true. */
  allowApproveDeny?: boolean;
}

export interface RenderedMessage {
  text: string;
  inline_keyboard: InlineKeyboardSpec;
}

/**
 * DESIGN §7 escalation card:
 *
 *   🔴 needs you · claude-code · omi-desktop @ mac-studio
 *   <question>
 *   <context lines…>
 *   Suggests: DENY — tell agent to rebase instead.
 *   [✅ Approve] [❌ Deny] / [💬 Reply] [😴 ▾] [🧠 Always…]
 */
export function renderEscalation(msg: EscalationRenderInput): RenderedMessage {
  const head = [`${severityMark(msg.severity)} needs you`];
  if (msg.sessionLabel) head.push(msg.sessionLabel);

  const lines = [head.join(" · "), msg.question.trim()];
  for (const line of msg.contextLines) {
    const trimmed = line.trim();
    if (trimmed) lines.push(trimmed);
  }
  if (msg.suggestedActionLabel) lines.push(`Suggests: ${msg.suggestedActionLabel}`);

  return {
    text: lines.join("\n"),
    inline_keyboard: escalationKeyboard(msg.escalationId, msg.allowApproveDeny !== false),
  };
}

export function escalationKeyboard(escalationId: string, allowApproveDeny = true): InlineKeyboardSpec {
  const rows: InlineKeyboardSpec = [];
  if (allowApproveDeny) {
    rows.push([
      { text: "✅ Approve", data: encodeCallback(CB.approve, escalationId) },
      { text: "❌ Deny", data: encodeCallback(CB.deny, escalationId) },
    ]);
  }
  rows.push([
    { text: "💬 Reply", data: encodeCallback(CB.reply, escalationId) },
    { text: "😴 ▾", data: encodeCallback(CB.snoozeMenu, escalationId) },
    { text: "🧠 Always…", data: encodeCallback(CB.alwaysMenu, escalationId) },
  ]);
  return rows;
}

export function snoozeKeyboard(escalationId: string): InlineKeyboardSpec {
  return [
    [
      { text: "1h", data: encodeCallback(CB.snooze1h, escalationId) },
      { text: "Tonight", data: encodeCallback(CB.snoozeTonight, escalationId) },
      { text: "Next digest", data: encodeCallback(CB.snoozeDigest, escalationId) },
    ],
    [{ text: "◀︎ Back", data: encodeCallback(CB.backToMain, escalationId) }],
  ];
}

export function alwaysKeyboard(escalationId: string): InlineKeyboardSpec {
  return [
    [{ text: "🧠 Always do this", data: encodeCallback(CB.alwaysAll, escalationId) }],
    [{ text: "…this repo only", data: encodeCallback(CB.alwaysRepo, escalationId) }],
    [
      { text: "Keep asking", data: encodeCallback(CB.alwaysKeep, escalationId) },
      { text: "◀︎ Back", data: encodeCallback(CB.backToMain, escalationId) },
    ],
  ];
}

/** The message an escalation is edited into once it is resolved. */
export function renderResolution(
  original: string,
  resolution: string,
  by = "david",
): RenderedMessage {
  const body = strikeHeader(original);
  return { text: `${body}\n— ${resolution} (${by})`, inline_keyboard: [] };
}

/** The message an escalation is edited into once snoozed. */
export function renderSnoozed(original: string, untilLabel: string): RenderedMessage {
  return {
    text: `${strikeHeader(original)}\n😴 snoozed until ${untilLabel}`,
    inline_keyboard: [],
  };
}

/** Dim the "needs you" header of a resolved card so the thread reads cleanly. */
function strikeHeader(original: string): string {
  const nl = original.indexOf("\n");
  if (nl < 0) return original;
  const head = original.slice(0, nl);
  return `${head.replace("needs you", "handled")}${original.slice(nl)}`;
}

/* -------------------------------------------------- Telegram reply markup */

export type CallbackButton = { text: string; callback_data: string };
export type InlineMarkup = { inline_keyboard: CallbackButton[][] };
export type ReplyMarkup = InlineMarkup | { force_reply: true };

export function toInlineKeyboard(rows: InlineKeyboardSpec): CallbackButton[][] {
  return rows.map((row) => row.map((b) => ({ text: b.text, callback_data: b.data })));
}

/** editMessageText accepts inline keyboards only — never a force reply. */
export function toInlineMarkup(spec: MessageSpec): InlineMarkup | undefined {
  if (!spec.inline_keyboard || !spec.inline_keyboard.length) return undefined;
  return { inline_keyboard: toInlineKeyboard(spec.inline_keyboard) };
}

export function toReplyMarkup(spec: MessageSpec): ReplyMarkup | undefined {
  const inline = toInlineMarkup(spec);
  if (inline) return inline;
  if (spec.force_reply) return { force_reply: true };
  return undefined;
}

/**
 * What an edit must send as reply_markup. Omitting it leaves the OLD keyboard
 * live, so a resolved card would keep tappable Approve/Deny — an empty
 * inline_keyboard is how Telegram is told to remove the buttons.
 */
export function editMarkup(spec: MessageSpec): InlineMarkup {
  return toInlineMarkup(spec) ?? { inline_keyboard: [] };
}

/* --------------------------------------------------------------- notifies */

export function renderNotify(text: string): MessageSpec {
  return { text };
}

/** The force-reply prompt behind the 💬 button. */
export function renderReplyPrompt(sessionLabel: string | null): MessageSpec {
  const who = sessionLabel ? ` to ${sessionLabel}` : "";
  return {
    text: `💬 Reply${who} — your next message replying to this goes verbatim to the agent.`,
    force_reply: true,
  };
}

/* ---------------------------------------------------- digest button markers */

/**
 * `sendDigest(markdown)` is a frozen string-only port, but DESIGN §7's digest
 * carries 👍/👎 and promotion buttons. The digest builder embeds buttons as
 * inline markers; the Telegram layer strips them and lifts them into a real
 * inline keyboard. Anything that renders the digest as plain text (web UI,
 * `/brief.md`, the `digests` table) sees clean markdown after `stripButtons`.
 */
const MARKER = /⟦btn:([^:⟦⟧]*):([^⟦⟧]*)⟧/g;

export function btnMarker(label: string, data: string): string {
  return `⟦btn:${label}:${data}⟧`;
}

export function stripButtons(markdown: string): string {
  return markdown.replace(MARKER, "").replace(/[ \t]+$/gm, "");
}

export function extractButtons(markdown: string): InlineButton2[] {
  const out: InlineButton2[] = [];
  for (const m of markdown.matchAll(MARKER)) out.push({ text: m[1]!, data: m[2]! });
  return out;
}

interface InlineButton2 {
  text: string;
  data: string;
}

/**
 * Render the digest for Telegram: markers become an inline keyboard laid out
 * `perRow` wide, and the visible text keeps the markdown clean.
 */
export function renderDigestMessage(markdown: string, perRow = 3): MessageSpec {
  const buttons = extractButtons(markdown);
  const rows: InlineKeyboardSpec = [];
  for (let i = 0; i < buttons.length; i += perRow) {
    rows.push(buttons.slice(i, i + perRow));
  }
  const spec: MessageSpec = { text: stripButtons(markdown).trim() };
  if (rows.length) spec.inline_keyboard = rows;
  return spec;
}
