/**
 * Where a message goes: forum mode (one topic per session) vs flat mode
 * (one anchor message per session, everything threaded under it).
 * Pure over the store; no API calls.
 */
import type { Store } from "../../store/db.ts";
import type { CarConfig } from "../../config/config.ts";
import { anchorKey } from "./outbox.ts";
import type { TelegramTarget, TelegramTargetKind } from "./types.ts";

export interface SessionRow {
  car_session_id: string;
  vendor: string;
  host: string;
  title: string | null;
  cwd: string | null;
  repo: string | null;
  repo_verified: number;
  state: string;
  last_event_at: string;
  last_heartbeat_at: string | null;
  expected_heartbeat_s: number | null;
  telegram_thread_id: string | null;
  muted_until: string | null;
}

export function getSession(store: Store, carSessionId: string): SessionRow | null {
  return (store.db
    .query("SELECT * FROM sessions WHERE car_session_id = ?")
    .get(carSessionId) as SessionRow | null) ?? null;
}

/** "claude-code · omi-desktop @ mac-studio" — vendor · title/repo @ host. */
export function sessionLabel(row: SessionRow | null): string | undefined {
  if (!row) return undefined;
  const what = row.title || shortRepo(row.repo) || shortCwd(row.cwd);
  const parts = [row.vendor];
  if (what) parts.push(what);
  return `${parts.join(" · ")} @ ${row.host}`;
}

function shortRepo(repo: string | null): string | null {
  if (!repo) return null;
  const seg = repo.split("/").filter(Boolean);
  return seg.length ? seg[seg.length - 1]! : repo;
}

function shortCwd(cwd: string | null): string | null {
  if (!cwd) return null;
  const seg = cwd.split("/").filter(Boolean);
  return seg.length ? seg[seg.length - 1]! : cwd;
}

/**
 * Resolve routing for a message about `carSessionId`.
 * - forum_mode: use the session's topic; ask the deliverer to create one if absent.
 * - flat mode: thread under the session's anchor message, or become the anchor.
 */
export function resolveTarget(
  store: Store,
  config: CarConfig,
  kind: TelegramTargetKind,
  carSessionId: string | null,
): TelegramTarget {
  const target: TelegramTarget = { kind, chat_id: config.telegram.chat_id, car_session_id: carSessionId };
  if (!carSessionId) return target;

  const row = getSession(store, carSessionId);
  if (config.telegram.forum_mode) {
    if (row?.telegram_thread_id) target.thread_id = row.telegram_thread_id;
    else target.create_topic = topicName(row, carSessionId);
    return target;
  }

  const anchor = store.kvGet<string>(anchorKey(carSessionId));
  if (anchor) target.reply_to_message_id = anchor;
  else target.become_anchor = true;
  return target;
}

export function topicName(row: SessionRow | null, carSessionId: string): string {
  if (!row) return carSessionId;
  const label = sessionLabel(row);
  return (label ?? carSessionId).slice(0, 120);
}

/** Sessions muted via /mute are silent until the mute expires. */
export function isMuted(store: Store, carSessionId: string | null, now: Date): boolean {
  if (!carSessionId) return false;
  const row = getSession(store, carSessionId);
  if (!row?.muted_until) return false;
  return row.muted_until > now.toISOString();
}
