import { readFileSync } from "node:fs";
import { AttentionService } from "../src/attention/service.ts";
import { DecisionPacket } from "../src/attention/contract.ts";
/**
 * Seeded local preview for the v3 operator console.
 *
 * This intentionally uses the real web app with the in-memory test store: it
 * never touches a user's CAR state, provider processes, or delivery channels.
 * Run-observation timestamps are relative to launch so freshness states remain
 * realistic during responsive review.
 * Run with: bun run scripts/ui-preview.ts
 */
import { CONTRACT_VERSION, parseEvent } from "../src/contract/events.ts";
import { FakeClock, memoryStore, testConfig } from "../test/fakes.ts";
import {
  buildDeps,
  mountApp,
  seedAction,
  seedDecision,
  seedEscalation,
  seedIncident,
  seedMemory,
  seedOutcome,
} from "../test/web/helpers.ts";

const clock = new FakeClock(new Date());
const store = memoryStore(clock);
const config = testConfig({
  state_dir: "/tmp/car-v3-ui-preview",
  http: { private_reads: false, ingest_tokens: { web: "preview-token" } },
  // The preview has no background poller; keep its seeded health fresh for a
  // normal review session while production retains the 15s default.
  agentctl_observer: { enabled: true, observe_all: true, interval_seconds: 3600 },
});
const deps = buildDeps({ store, config });
const previewNow = Date.now();

store.upsertAgentRun({
  executionId: "exec-preview-cursor",
  agent: "cursor",
  authority: "native",
  mode: "direct",
  state: "running",
  liveness: "healthy",
  labels: ["ui-polish"],
  title: "UI polish audit",
  repo: "github.com/Git-on-my-level/codex-autorunner",
  startedAt: "2026-08-30T13:42:00Z",
  updatedAt: new Date(previewNow - 2 * 60_000).toISOString(),
  durationSeconds: 1080,
  observationState: "observed",
  raw: { adapter: "cursor", authority: "native" },
});
store.upsertAgentRun({
  executionId: "exec-preview-omp",
  agent: "omp",
  authority: "native",
  mode: "direct",
  state: "completed",
  liveness: "exited",
  labels: ["responsive-check"],
  title: "Responsive layout check",
  startedAt: "2026-08-30T13:51:00Z",
  updatedAt: new Date(previewNow - 12 * 60_000).toISOString(),
  terminalAt: new Date(previewNow - 12 * 60_000).toISOString(),
  durationSeconds: 300,
  observationState: "observed",
  raw: { adapter: "omp", authority: "native" },
});
store.upsertAgentRun({
  executionId: "exec-preview-codex-stale",
  agent: "codex",
  authority: "native",
  mode: "direct",
  state: "running",
  liveness: "unreachable",
  labels: ["migration-review"],
  title: "Migration readiness review",
  startedAt: "2026-08-30T10:00:00Z",
  updatedAt: new Date(previewNow - 3 * 60 * 60_000).toISOString(),
  durationSeconds: 3900,
  observationState: "stale",
  raw: { adapter: "codex", authority: "native" },
});
store.kvSet("agentctl.observer.health", {
  state: "ok",
  observed_at: new Date(previewNow).toISOString(),
  run_count: 3,
  coverage_degraded: false,
});

function event(input: {
  key: string;
  title: string;
  body: string;
  type: "attention.question" | "attention.error" | "progress" | "artifact";
  severity: "info" | "notice" | "attention" | "urgent";
  requiresResponse: boolean;
  vendor?: "claude-code" | "codex" | "hermes";
  repo?: string;
}) {
  return store.ingestEvent(parseEvent({
    contract: CONTRACT_VERSION,
    idempotency_key: input.key,
    ts: clock.now().toISOString(),
    source: { vendor: input.vendor ?? "hermes", host: "david-mac", adapter: "preview" },
    session: {
      vendor: input.vendor ?? "hermes",
      native_id: `preview-${input.key}`,
      host: "david-mac",
      repo: input.repo ?? "github.com/Git-on-my-level/codex-autorunner",
      title: input.title,
    },
    type: input.type,
    severity: input.severity,
    requires_response: input.requiresResponse,
    title: input.title,
    body: input.body,
    response_channel: input.requiresResponse ? { kind: "file", hint: { path: "/tmp/car-v3-ui-preview/replies" } } : undefined,
  }));
}

const release = event({
  key: "release-approval",
  title: "Approve release candidate deployment?",
  body: "The provider contract suite, migration dry-run, and safety checks passed. The v2 archive remains a human-owned cutover step.",
  type: "attention.question",
  severity: "urgent",
  requiresResponse: true,
});
clock.advance(4 * 60_000);
const failed = event({
  key: "delivery-failure",
  title: "Telegram receipt is delivery-uncertain",
  body: "The remote send returned before the durable receipt was committed. CAR will not silently resend a possibly delivered message.",
  type: "attention.error",
  severity: "attention",
  requiresResponse: true,
  vendor: "codex",
});
clock.advance(7 * 60_000);
event({
  key: "contract-pass",
  title: "Provider contract suite passed",
  body: "Native and Hermes provider fixtures passed lifecycle, timeout, and terminal-record assertions.",
  type: "artifact",
  severity: "notice",
  requiresResponse: false,
  vendor: "hermes",
});
clock.advance(9 * 60_000);
event({
  key: "migration-progress",
  title: "v2 import rehearsal completed",
  body: "One-way import completed without creating a second lifecycle authority.",
  type: "progress",
  severity: "info",
  requiresResponse: false,
  vendor: "claude-code",
});

const releaseIncident = seedIncident(store.db, clock, {
  car_session_id: release.car_session_id,
  opened_by_event: release.event_id,
  state: "escalated",
  summary: "Release candidate needs a final operator decision",
  dedupe_class: "release-cutover",
  llm_runs: 1,
});
store.db.query("UPDATE events SET incident_id = ?, triage_state = 'escalated' WHERE id = ?").run(releaseIncident, release.event_id);
const releaseDecision = seedDecision(store.db, clock, releaseIncident, {
  disposition: "escalate",
  action_class: "release.deploy",
  action_args_json: JSON.stringify({ channel: "candidate", archive_v2_after_validation: false }),
  rationale: "All machine-verifiable checks passed, but activating v3 and archiving v2 are deliberately human-owned decisions.",
  model: "hermes/operator-primary",
  tokens_in: 1240,
  tokens_out: 218,
  cost_usd: 0.0068,
});
seedAction(store.db, releaseDecision, {
  class: "probe.release_readiness",
  policy_verdict: "escalate",
  state: "ok",
  result_json: JSON.stringify({ tests: 702, migration: "clean", safety_kernel: "ready" }),
});
seedEscalation(store.db, clock, releaseIncident, {
  severity: "urgent",
  question: "Approve the v3 release candidate while keeping the v2 archive step manual?",
  suggested_action_json: JSON.stringify({ label: "Approve candidate only", effect: "release.deploy" }),
  state: "pending",
  telegram_message_id: "tg-preview-1042",
  sent_at: clock.now().toISOString(),
});
seedOutcome(store.db, clock, { decision_id: releaseDecision, verdict: "pending", note: "Waiting for David." });
store.audit("provider:hermes", "decision.made", "decision", releaseDecision, { disposition: "escalate" });

const deliveryIncident = seedIncident(store.db, clock, {
  car_session_id: failed.car_session_id,
  opened_by_event: failed.event_id,
  state: "open",
  summary: "Confirm an uncertain Telegram delivery before retrying",
  dedupe_class: "telegram-delivery",
  llm_runs: 0,
});
store.db.query("UPDATE events SET incident_id = ?, triage_state = 'escalated' WHERE id = ?").run(deliveryIncident, failed.event_id);

seedMemory(store.db, clock, {
  tier: "rule",
  scope: { repo: "Git-on-my-level/codex-autorunner" },
  content: { statement: "Always keep the v2 archive step human-owned", action_class: "migration.archive_v2" },
  confidence: 0.94,
  evidence_confirm: 8,
  autonomy: "granted",
  authored_by: "david",
});
seedMemory(store.db, clock, {
  tier: "rule",
  scope: { provider: "hermes" },
  content: { statement: "Prefer a bounded probe before escalating delivery uncertainty" },
  confidence: 0.71,
  evidence_confirm: 4,
  evidence_override: 1,
  autonomy: "suggest",
  authored_by: "outcome-learning",
});
seedMemory(store.db, clock, {
  tier: "note",
  scope: { repo: "Git-on-my-level/codex-autorunner" },
  content: { text: "Decision summaries should lead with the human-owned boundary." },
  confidence: 1,
  authored_by: "david",
});
seedMemory(store.db, clock, {
  tier: "episode",
  status: "pending",
  scope: { provider: "hermes", repo: "Git-on-my-level/codex-autorunner" },
  content: { text: "Delivery-uncertain receipts should be reviewed before retrying." },
  confidence: 0.82,
  evidence_confirm: 2,
  authored_by: "hermes",
});

store.db.query("INSERT INTO digests (day, rendered_md, sent_at) VALUES (?, ?, ?)").run(
  "2026-08-30",
  "# CAR digest — Aug 30\n## Needs you\n- Approve the v3 release candidate; v2 archive remains manual.\n- Confirm the uncertain Telegram delivery before any retry.\n## Changes\n- Provider contract suite passed.\n- Migration rehearsal completed without dual writers.\n- 702 checks passed.",
  null,
);
const uncertainDigestOutbox = store.enqueueOutbox("telegram", { chat_id: "preview" }, { kind: "digest", day: "2026-08-30" });
store.attachDigestOutbox("2026-08-30", uncertainDigestOutbox);
store.db.query("UPDATE outbox SET state = 'uncertain', recovery_state = 'remote_delivery_unknown' WHERE id = ?").run(uncertainDigestOutbox);
store.db.query("INSERT INTO digests (day, rendered_md, sent_at) VALUES (?, ?, ?)").run(
  "2026-08-29",
  "# CAR digest — Aug 29\n## Completed\n- Fenced leases enabled for provider and effect claims.\n- Core safety denied one stale claim.\n## Delivery\n- Telegram digest delivered with a durable receipt.",
  "2026-08-29T12:30:00Z",
);
const deliveredDigestOutbox = store.enqueueOutbox("telegram", { chat_id: "preview" }, { kind: "digest", day: "2026-08-29" });
store.attachDigestOutbox("2026-08-29", deliveredDigestOutbox);
store.db.query("UPDATE outbox SET state = 'delivered', sent_message_id = 'tg-preview-991', result_json = ? WHERE id = ?").run(
  JSON.stringify({ receipt_at: "2026-08-29T12:30:00Z", message_id: "tg-preview-991" }),
  deliveredDigestOutbox,
);

// Seed the current decision experience as well as the advanced legacy inspectors.
const attention = new AttentionService(store, config, deps.channel);
const owner = { workspaceId: config.attention.workspace_id, clientId: "preview-mac", host: "preview-host" };
const packet = DecisionPacket.parse(JSON.parse(readFileSync(new URL("../examples/attention/migration-decision.json", import.meta.url), "utf8")));
attention.raise(owner, "preview-needs-you", packet);
attention.raise(owner, "preview-preparing", DecisionPacket.parse({ goal: "Repair CI", blocker: "Failure cause unknown", question: "Should we change the supported runtime?" }));
const waiting = attention.raise(owner, "preview-waiting", { ...packet, question: "Approve the staged migration plan?" });
attention.answer(waiting.id, waiting.revision, "human:preview", { option_id: "preserve" });
const finished = attention.raise(owner, "preview-complete", { ...packet, question: "Preserve the compatibility test fixture?" });
const received = attention.answer(finished.id, finished.revision, "human:preview", { text: "Keep the fixture" });
attention.acknowledge(owner, finished.id, received.id, "received");
attention.acknowledge(owner, finished.id, received.id, "resolved", "Source confirmed work resumed");
const app = mountApp(deps);
const port = Number(process.env.CAR_UI_PREVIEW_PORT ?? 7194);
const server = Bun.serve({ hostname: "127.0.0.1", port, fetch: app.fetch });
console.log(`CAR v3 UI preview: ${server.url}ui`);
