/**
 * WS-D — scheduler loop: the unconditional daily digest, the silence watchdog,
 * snooze expiry, and the nightly memory consolidation slot.
 *
 * Ticks every 60s off `deps.store.clock`, so a FakeClock drives a whole week of
 * behaviour in a test. Every "have I already done this today?" question is
 * answered from a row (kv / digests / events.idempotency_key), never from
 * process memory — the daemon is crash-only.
 */
import type { ChannelPort, DaemonDeps, EscalationMessage, Loop } from "../ports.ts";
import type { Store } from "../store/db.ts";
import { suggestedLabel } from "../surfaces/telegram/rows.ts";
import { buildDigest, type DigestData } from "./build.ts";
import { renderDigest, renderMissedDay } from "./render.ts";
import { runWatchdog, type StuckLine } from "./watchdog.ts";
import { localDay, localHhMm, parseHhMm, shiftHhMm } from "./time.ts";

export const TICK_MS = 60_000;
const WATCHDOG_INTERVAL_MS = 15 * 60_000;
const CONSOLIDATION_LEAD_MINUTES = 60;
const MAX_BACKFILL_DAYS = 7;

const KV_LAST_DIGEST = "scheduler.last_digest_day";
const KV_LAST_CONSOLIDATION = "scheduler.last_consolidation_day";
const KV_LAST_WATCHDOG = "scheduler.last_watchdog_at";

interface ChannelPortWithTrackedDigest extends ChannelPort {
  sendDigestTracked(markdown: string, day: string, heldOutboxIds: number[]): number;
}

export interface DigestScheduler extends Loop {
  /** One scheduler pass. Exposed so tests drive it with a fake clock. */
  tick(): Promise<void>;
  /** Build + persist + send today's digest now, regardless of the clock. */
  runDigestNow(): Promise<DigestData>;
}

export function createDigestScheduler(
  deps: DaemonDeps,
  consolidationJob: () => Promise<void>,
): DigestScheduler {
  const { store, config } = deps;
  let timer: ReturnType<typeof setInterval> | null = null;
  let running = false;

  async function runConsolidation(day: string): Promise<void> {
    if (store.kvGet<string>(KV_LAST_CONSOLIDATION) === day) return;
    store.kvSet(KV_LAST_CONSOLIDATION, day);
    try {
      await consolidationJob();
      store.audit("daemon", "consolidation.ran", "daemon", "card", { day });
    } catch (err) {
      store.audit("daemon", "consolidation.failed", "daemon", "card", { day, error: String(err) });
    }
  }

  async function buildAndSend(stuck: StuckLine[]): Promise<DigestData> {
    const now = store.clock.now();
    const day = localDay(now);
    // Nightly consolidation is "before the digest" by contract; if the clock
    // never crossed its slot (daemon was down), run it here rather than skip it.
    await runConsolidation(day);

    store.reconcileDigestReceipts();
    const missedDays = backfillMissedDays(store, day);
    const data = await buildDigest(store, config, { stuck, missedDays });
    const existing = store.getDigest(day);
    const markdown = existing?.rendered_md ?? renderDigest(data, now);
    const heldIds = existing ? parseHeldIds(existing.held_outbox_ids_json) : data.held.map((h) => h.outboxId);

    if (!existing) store.recordDigest(day, markdown, heldIds);

    // A delivered/failed/uncertain canonical row is terminal. The former is
    // reconciled above; the latter two stay visible for explicit operator
    // reconciliation and are never silently duplicated by the scheduler.
    const current = store.getDigest(day);
    const outbox = current?.outbox_id
      ? (store.db.query("SELECT state FROM outbox WHERE id = ?").get(current.outbox_id) as { state: string } | null)
      : null;
    if (current?.sent_at || outbox && ["pending", "sending", "deferred", "failed", "uncertain"].includes(outbox.state)) {
      return data;
    }

    const tracked = deps.channel as ChannelPortWithTrackedDigest;
    if (typeof tracked.sendDigestTracked === "function") {
      const outboxId = tracked.sendDigestTracked(markdown, day, heldIds);
      store.attachDigestOutbox(day, outboxId);
      store.reconcileDigestReceipts();
    } else {
      // Compatibility fakes/ports may only expose the historical void method.
      // They can observe the enqueue request, but cannot claim delivery truth.
      deps.channel.sendDigest(markdown);
    }
    return data;
  }

  async function tick(): Promise<void> {
    const now = store.clock.now();
    const day = localDay(now);

    // Receipt reconciliation is also run at scheduler start so a crash after
    // the outbox terminal write but before projection side effects is replayed.
    store.reconcileDigestReceipts();

    expireSnoozes(deps);

    let stuck: StuckLine[] = [];
    let watchdogRan = false;
    const lastWatchdog = store.kvGet<string>(KV_LAST_WATCHDOG);
    if (!lastWatchdog || now.getTime() - Date.parse(lastWatchdog) >= WATCHDOG_INTERVAL_MS) {
      stuck = runWatchdog(store, config).stuck;
      store.kvSet(KV_LAST_WATCHDOG, now.toISOString());
      watchdogRan = true;
    }

    // Nightly consolidation slot: an hour before the digest.
    const digestMinutes = toMinutes(config.telegram.digest_time);
    const consolidationTime = shiftHhMm(config.telegram.digest_time, -CONSOLIDATION_LEAD_MINUTES);
    const consolidationMinutes = toMinutes(consolidationTime);
    const nowMinutes = toMinutes(localHhMm(now));
    if (consolidationMinutes <= digestMinutes && nowMinutes >= consolidationMinutes) {
      await runConsolidation(day);
    }

    const tracked = deps.channel as ChannelPortWithTrackedDigest;
    const recoverableDigest = typeof tracked.sendDigestTracked === "function" && digestNeedsEnqueue(store, day);
    if (nowMinutes >= digestMinutes && (recoverableDigest || !digestExists(store, day))) {
      // Always digest against a fresh watchdog read.
      if (!watchdogRan) {
        stuck = runWatchdog(store, config).stuck;
        store.kvSet(KV_LAST_WATCHDOG, now.toISOString());
      }
      await buildAndSend(stuck);
    }
  }

  return {
    name: "scheduler",
    async tick() {
      await tick();
    },
    async runDigestNow() {
      return buildAndSend(runWatchdog(store, config).stuck);
    },
    start() {
      timer = setInterval(() => {
        if (running) return;
        running = true;
        void tick()
          .catch((err: unknown) => {
            store.audit("daemon", "scheduler.tick_error", "daemon", "card", { error: String(err) });
          })
          .finally(() => {
            running = false;
          });
      }, TICK_MS);
    },
    stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
}

/**
 * Incidents whose snooze has run out are reopened and resurfaced. Where an
 * escalation exists we re-send the full card (buttons and all) rather than a
 * bare notice: a resurfaced ask David cannot act on is not a resurfaced ask.
 */
export function expireSnoozes(deps: DaemonDeps): number {
  const { store } = deps;
  const now = store.clock.now().toISOString();
  const rows = store.db
    .query(
      `SELECT id, car_session_id, summary FROM incidents
       WHERE state = 'snoozed' AND snooze_until IS NOT NULL AND snooze_until <= ?`,
    )
    .all(now) as { id: string; car_session_id: string | null; summary: string }[];

  for (const row of rows) {
    store.db.query("UPDATE incidents SET state = 'open', snooze_until = NULL WHERE id = ?").run(row.id);
    store.db
      .query("UPDATE escalations SET state = 'pending' WHERE incident_id = ? AND state = 'snoozed'")
      .run(row.id);

    const esc = store.db
      .query(
        `SELECT id, severity, question, suggested_action_json FROM escalations
         WHERE incident_id = ? AND state = 'pending' ORDER BY created_at DESC LIMIT 1`,
      )
      .get(row.id) as
      | { id: string; severity: string; question: string; suggested_action_json: string | null }
      | null;

    if (esc) {
      deps.channel.sendEscalation({
        escalationId: esc.id,
        notificationRevision: `wake:${now}`,
        incidentId: row.id,
        carSessionId: row.car_session_id,
        severity: esc.severity as EscalationMessage["severity"],
        question: esc.question,
        contextLines: ["⏰ Snooze expired — this is back."],
        ...(suggestedLabel(esc.suggested_action_json)
          ? { suggestedActionLabel: suggestedLabel(esc.suggested_action_json)! }
          : {}),
      });
    } else {
      deps.channel.sendNotify(
        `⏰ Snooze expired — still needs you: ${row.summary || row.id}`,
        row.car_session_id ?? undefined,
      );
    }
    store.audit("daemon", "incident.snooze_expired", "incident", row.id, {
      resurfaced_as: esc ? "escalation" : "notify",
    });
  }
  return rows.length;
}

function digestExists(store: Store, day: string): boolean {
  return Boolean(store.db.query("SELECT day FROM digests WHERE day = ?").get(day));
}

function digestNeedsEnqueue(store: Store, day: string): boolean {
  const row = store.getDigest(day);
  if (!row || row.sent_at) return false;
  if (!row.outbox_id) return true;
  const outbox = store.db.query("SELECT state FROM outbox WHERE id = ?").get(row.outbox_id) as { state: string } | null;
  return !outbox;
}

function parseHeldIds(value: string): number[] {
  try {
    const parsed = JSON.parse(value) as unknown;
    return Array.isArray(parsed)
      ? parsed.filter((id): id is number => typeof id === "number" && Number.isInteger(id) && id > 0)
      : [];
  } catch {
    return [];
  }
}

/**
 * Non-negotiable #2, archive edition: days the daemon was down get an explicit
 * placeholder row rather than a hole in the digest history.
 */
function backfillMissedDays(store: Store, today: string): string[] {
  const last = store.kvGet<string>(KV_LAST_DIGEST);
  if (!last || last >= today) return [];
  const missed: string[] = [];
  const cursor = new Date(`${last}T12:00:00`);
  for (let i = 0; i < MAX_BACKFILL_DAYS; i++) {
    cursor.setDate(cursor.getDate() + 1);
    const day = localDay(cursor);
    if (day >= today) break;
    if (digestExists(store, day)) continue;
    store.db
      .query("INSERT INTO digests (day, rendered_md, sent_at) VALUES (?, ?, NULL)")
      .run(day, renderMissedDay(day));
    missed.push(day);
  }
  if (missed.length) store.audit("daemon", "digest.backfilled", "digest", today, { missed });
  return missed;
}

function toMinutes(hhmm: string): number {
  const { hours, minutes } = parseHhMm(hhmm);
  return hours * 60 + minutes;
}

export { buildDigest, markHeldDelivered } from "./build.ts";
export { renderDigest, isQuietDay } from "./render.ts";
export { runWatchdog } from "./watchdog.ts";
export type { DigestData } from "./build.ts";
export type { StuckLine, WatchdogResult } from "./watchdog.ts";
