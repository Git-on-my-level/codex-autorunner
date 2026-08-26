/**
 * WS-D owns src/digest/: unconditional daily digest builder, silence watchdog,
 * snooze expiry, scheduling of memory consolidation.
 * Scaffold stub: hourly no-op tick with audit heartbeat.
 */
import type { DaemonDeps, Loop } from "../ports.ts";

export function createDigestScheduler(deps: DaemonDeps, _consolidationJob: () => Promise<void>): Loop {
  let timer: ReturnType<typeof setInterval> | null = null;
  return {
    name: "scheduler",
    start() {
      timer = setInterval(() => {
        deps.store.audit("daemon", "scheduler.tick", "daemon", "card", {});
      }, 60 * 60 * 1000);
    },
    stop() {
      if (timer) clearInterval(timer);
    },
  };
}
