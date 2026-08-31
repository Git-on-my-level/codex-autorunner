/**
 * CAR v3 composition root.
 *
 * SQLite and provider-owned state are authoritative. The daemon owns only
 * restartable workers and provider processes; no surface owns routing,
 * authorization, provider topology, or effect execution.
 */
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { loadConfig, dbPath, type CarConfig } from "./config/config.ts";
import { validateProviderTopology } from "./config/provider_topology.ts";
import { openStore, type Store } from "./store/db.ts";
import type { DaemonDeps, Loop, PolicyPort } from "./ports.ts";

import { createIngestServer } from "./ingest/server.ts";
import { createPolicy } from "./policy/index.ts";
import { createMemory } from "./memory/index.ts";
import { createTelegram } from "./surfaces/telegram/index.ts";
import { createDigestScheduler } from "./digest/index.ts";
import { createActionBus } from "./actions/index.ts";
import { createWebUi } from "./surfaces/web/index.ts";
import { createDeadmanLoop } from "./ops/deadman.ts";
import { createAgentctlObserverLoop } from "./ops/agentctl_observer.ts";
import { createProviderHost } from "./providers/host.ts";
import { createProviderObservationLoop } from "./providers/observations.ts";
import { createRouter } from "./router/index.ts";
import { createSafetyKernel, SqlSafetyLedger } from "./safety/index.ts";
import { createEffectExecutor } from "./effects/index.ts";
import { createCoreEffectAdapters } from "./effects/adapters.ts";

/** Provider judgment is advisory; this seam performs no second authorization. */
const CORE_EXECUTION_POLICY: PolicyPort = {
  check: () => "auto",
  gate: () => null,
  escalateOnly: () => false,
  autoApprovalBlock: () => null,
};

export async function startDaemon(configPath?: string): Promise<{ stop: () => Promise<void> }> {
  const config: CarConfig = loadConfig(configPath);
  validateProviderTopology(config);
  mkdirSync(config.state_dir, { recursive: true });
  mkdirSync(join(config.state_dir, "memory"), { recursive: true });
  mkdirSync(join(config.state_dir, "replies"), { recursive: true });
  const store: Store = openStore(dbPath(config));
  const daemonClaim = store.claimDaemonOwner(`pid:${process.pid}:${randomUUID()}`, 30);
  if (!daemonClaim) {
    store.db.close();
    throw new Error(`another CAR v3 daemon owns ${dbPath(config)}`);
  }
  store.recoverExpiredClaims();

  const safety = createSafetyKernel({
    ledger: new SqlSafetyLedger(store),
    clock: store.clock,
    limits: {
      max_attempts_per_window: config.safety.max_effects_per_hour,
      attempt_window_ms: 60 * 60_000,
      max_failures_per_window: config.safety.max_failures_per_10m,
      failure_window_ms: 10 * 60_000,
      max_spend_usd: config.safety.max_effect_spend_usd,
      dedupe_window_ms: config.safety.dedupe_minutes * 60_000,
      default_lease_ms: config.safety.effect_lease_seconds * 1_000,
    },
    audit: (verb, objectId, detail) => store.audit("safety", verb, "safety", objectId, detail),
  });

  // Compatibility memory/policy remain projections for the current web and
  // digest surfaces. They are not routing or execution authority.
  const legacyPolicy = createPolicy(store, config);
  const { reader: memoryReader, writer: memoryWriter, consolidationJob } = createMemory(store, config);
  const actions = createActionBus(store, config, CORE_EXECUTION_POLICY);
  const channel = createTelegram(store, config, actions, memoryWriter, safety);
  const providerHost = createProviderHost(store, config);
  const effects = createEffectExecutor({
    kernel: safety,
    owner: "daemon-effects",
    leaseMs: config.safety.effect_lease_seconds * 1_000,
    adapters: createCoreEffectAdapters(store, actions, channel),
  });
  const router = createRouter({
    store,
    config,
    registry: providerHost.registry,
    ensureProvider: (resolved, capability) => providerHost.ensure(resolved, capability),
    channel,
    safety,
    effects,
    leaseSeconds: config.triage.lease_seconds,
  });

  const deps: DaemonDeps = {
    store,
    config,
    triage: { tick: async () => (await router.tick()).processed },
    actions,
    channel,
    memoryReader,
    memoryWriter,
    policy: legacyPolicy,
  };

  const loops: Loop[] = [
    daemonOwnershipLoop(store, daemonClaim, safety),
    createIngestServer(deps, [createWebUi(deps)]),
    channel.loop,
    router,
    createProviderObservationLoop({
      store,
      config,
      registry: providerHost.registry,
      ensureProvider: (resolved, capability) => providerHost.ensure(resolved, capability),
    }),
    createAgentctlObserverLoop(store, config),
    createDigestScheduler(deps, consolidationJob),
    providerHealthLoop(store, providerHost),
    createDeadmanLoop(store, config),
  ];
  const started: Loop[] = [];
  try {
    for (const loop of loops) {
      await loop.start();
      started.push(loop);
    }
  } catch (error) {
    for (const loop of [...started].reverse()) await safeStop(store, loop);
    await providerHost.close();
    store.releaseDaemonOwner(daemonClaim);
    store.db.close();
    throw error;
  }
  store.audit("daemon", "daemon.started", "daemon", "card", {
    pid: process.pid,
    router: router.name,
    provider_defaults: config.providers.defaults,
  });

  let stopped = false;
  return {
    stop: async () => {
      if (stopped) return;
      stopped = true;
      for (const loop of [...loops].reverse()) await safeStop(store, loop);
      await providerHost.close();
      store.audit("daemon", "daemon.stopped", "daemon", "card", {});
      store.releaseDaemonOwner(daemonClaim);
      store.db.close();
    },
  };
}

function daemonOwnershipLoop(
  store: Store,
  claim: { owner: string; token: string },
  safety: ReturnType<typeof createSafetyKernel>,
): Loop {
  let timer: ReturnType<typeof setInterval> | null = null;
  let lost = false;
  const renew = () => {
    if (lost || store.renewDaemonOwner(claim, 30)) return;
    lost = true;
    safety.panic("daemon ownership lease lost");
    store.audit("daemon", "daemon.owner_lost", "daemon", "card", { owner: claim.owner });
    // The CLI signal handler performs orderly shutdown. Continuing to serve
    // after losing this fence would create two lifecycle authorities.
    process.kill(process.pid, "SIGTERM");
  };
  return {
    name: "daemon-owner",
    start() {
      timer = setInterval(renew, 10_000);
    },
    stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
}

function providerHealthLoop(store: Store, host: ReturnType<typeof createProviderHost>): Loop {
  let timer: ReturnType<typeof setInterval> | null = null;
  let running = false;
  const tick = async () => {
    if (running) return;
    running = true;
    try {
      await host.refreshHealth();
    } catch (error) {
      store.audit("daemon", "provider.health_failed", "daemon", "card", { error: String(error) });
    } finally {
      running = false;
    }
  };
  return {
    name: "provider-health",
    start() {
      timer = setInterval(() => void tick(), 30_000);
    },
    stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
}

async function safeStop(store: Store, loop: Loop): Promise<void> {
  try {
    await loop.stop();
  } catch (error) {
    store.audit("daemon", "loop.stop_failed", "daemon", loop.name, { error: String(error) });
  }
}
