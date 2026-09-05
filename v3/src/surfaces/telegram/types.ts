/**
 * Wire shapes for the Telegram outbox. Everything the bot ever sends is first
 * written to `outbox` as a (target, spec) pair; the deliverer is the only thing
 * that talks to the Telegram API. Buttons only ever write rows (the v2 lesson).
 *
 * These types are the body/target JSON contract between the enqueue side
 * (ChannelPort methods, callback handlers) and the deliverer.
 */

export const TELEGRAM_CHANNEL = "telegram";

/** A single inline keyboard button. `data` is the callback_data (≤64 bytes). */
export interface InlineButton {
  text: string;
  data: string;
}

export type InlineKeyboardSpec = InlineButton[][];

/** A rich message spec: what to render, independent of transport. */
export interface MessageSpec {
  text: string;
  inline_keyboard?: InlineKeyboardSpec;
  /** Ask Telegram to open the reply composer pre-targeted at this message. */
  force_reply?: boolean;
  /** Send silently (keep-informed / ticker updates). */
  disable_notification?: boolean;
}

export type TelegramTargetKind = "escalation" | "notify" | "digest" | "edit" | "ticker";

export interface TelegramTarget {
  kind: TelegramTargetKind;
  /** Chat the message belongs to; resolved from config at enqueue time. */
  chat_id?: string;
  /** Forum mode: topic (message thread) id for the session. */
  thread_id?: string | null;
  /** Flat mode: anchor message this should thread under. */
  reply_to_message_id?: string | null;
  /** For kind='edit'/'ticker': the message to edit in place. */
  edit_message_id?: string | null;
  escalation_id?: string | null;
  incident_id?: string | null;
  car_session_id?: string | null;
  /**
   * Set upstream (quiet hours / policy). The deliverer does NOT push these;
   * it parks them as `deferred` and the next digest folds them in.
   */
  queue_for_digest?: boolean;
  /** Forum mode: create the topic with this name if the session has none yet. */
  create_topic?: string;
  /** Make this message the session's flat-mode anchor once delivered. */
  become_anchor?: boolean;
}

/** Outbox row as this module reads it. */
export interface OutboxRow {
  id: number;
  channel: string;
  target_json: string;
  body_json: string;
  state: string;
  attempts: number;
  next_attempt_at: string;
  sent_message_id: string | null;
  created_at: string;
  claim_owner?: string | null;
  claim_token?: string | null;
  lease_until?: string | null;
}

/** Outbox states this module uses. `deferred` = held for the digest. */
export const OUTBOX_STATE = {
  pending: "pending",
  sent: "delivered",
  failed: "failed",
  dead: "failed",
  deferred: "deferred",
} as const;

export interface SendResult {
  message_id?: string;
  thread_id?: string;
}

/**
 * The single seam over the Telegram API. Tests inject a fake; the real one is
 * built from grammY's `bot.api` inside bot.ts. Nothing else in this module
 * imports grammY.
 */
export type TelegramSendFn = (target: TelegramTarget, spec: MessageSpec) => Promise<SendResult>;
