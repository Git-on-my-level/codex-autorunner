/**
 * Composition root: one Bun process, five cooperating loops over the shared store.
 * Crash-only: no authoritative in-memory state; kill -9 and restart resumes from tables.
 *
 * Scaffold wires no-op module implementations; workstreams replace the factory
 * functions in their own modules (see registry below) without touching this file's
 * loop structure.
 */
import { loadConfig, dbPath, type CarConfig } from "./config/config.ts";
import { openStore, type Store } from "./store/db.ts";
import type { DaemonDeps, Loop } from "./ports.ts";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

// Module factories — each workstream owns exactly one of these modules.
import { createIngestServer } from "./ingest/server.ts"; // WS-A
import { createTriage } from "./triage/index.ts"; // WS-B
import { createPolicy } from "./policy/index.ts"; // WS-B
import { createMemory } from "./memory/index.ts"; // WS-C
import { createTelegram } from "./surfaces/telegram/index.ts"; // WS-D
import { createDigestScheduler } from "./digest/index.ts"; // WS-D
import { createActionBus } from "./actions/index.ts"; // WS-E
import { createWebUi } from "./surfaces/web/index.ts"; // WS-F

export async function startDaemon(configPath?: string): Promise<{ stop: () => Promise<void> }> {
  const config: CarConfig = loadConfig(configPath);
  mkdirSync(config.state_dir, { recursive: true });
  mkdirSync(join(config.state_dir, "memory"), { recursive: true });
  mkdirSync(join(config.state_dir, "replies"), { recursive: true });
  const store: Store = openStore(dbPath(config));

  const policy = createPolicy(store, config);
  const { reader: memoryReader, writer: memoryWriter, consolidationJob } = createMemory(store, config);
  const actions = createActionBus(store, config, policy);
  const channel = createTelegram(store, config, actions, memoryWriter);
  const triage = createTriage(store, config, {
    policy,
    actions,
    channel,
    memoryReader,
    memoryWriter,
  });

  const deps: DaemonDeps = {
    store,
    config,
    triage,
    actions,
    channel,
    memoryReader,
    memoryWriter,
    policy,
  };

  const loops: Loop[] = [
    createIngestServer(deps, [createWebUi(deps)]), // HTTP: ingest + web UI + /brief.md
    channel.loop, // Telegram long-poll + outbox deliverer
    triageLoop(deps),
    createDigestScheduler(deps, consolidationJob), // digest + watchdog + consolidation + snooze expiry
  ];

  for (const loop of loops) await loop.start();
  store.audit("daemon", "daemon.started", "daemon", "card", { pid: process.pid });

  return {
    stop: async () => {
      for (const loop of [...loops].reverse()) await loop.stop();
      store.audit("daemon", "daemon.stopped", "daemon", "card", {});
      store.db.close();
    },
  };
}

function triageLoop(deps: DaemonDeps): Loop {
  let timer: ReturnType<typeof setInterval> | null = null;
  let running = false;
  return {
    name: "triage",
    start() {
      timer = setInterval(async () => {
        if (running) return; // no overlapping ticks
        running = true;
        try {
          await deps.triage.tick();
        } catch (err) {
          deps.store.audit("daemon", "triage.tick_error", "daemon", "card", { error: String(err) });
        } finally {
          running = false;
        }
      }, 2000);
    },
    stop() {
      if (timer) clearInterval(timer);
    },
  };
}
