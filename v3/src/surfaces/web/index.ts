/**
 * WS-F owns src/surfaces/web/: server-rendered JSX (hono/jsx) power-user UI —
 * inbox, incidents, memory browser, policy viewer, digest archive — plus
 * GET /brief.md for other agents to curl. Zero external assets: no CDN
 * scripts, no htmx: plain HTML forms/links + inline CSS in a shared layout,
 * so it works fully offline on localhost.
 *
 * Reads go straight to SELECTs on deps.store.db (queries.ts). Every mutating
 * POST goes through a documented Store/MemoryWriter method or a direct UPDATE
 * paired with an explicit store.audit() call (writes.ts) — no bare writes.
 */
import { Hono } from "hono";
import { readFileSync } from "node:fs";
import { parse as parseToml } from "smol-toml";
import type { DaemonDeps } from "../../ports.ts";
import { Vendor, Severity } from "../../contract/events.ts";
import { charterPath, policyPath } from "../../config/config.ts";
import {
  listEvents,
  listIncidents,
  getIncidentChain,
  listRules,
  listNotes,
  listPendingMemories,
  listDigests,
  listAgentRuns,
  getAgentObserverHealth,
  getAgentRunSummary,
} from "./queries.ts";
import type { AgentRunStateFilter } from "./queries.ts";
import {
  InboxPage,
  IncidentsListPage,
  IncidentDetailPage,
  MemoryPage,
  PolicyPage,
  DigestsPage,
  RunsPage,
  LoginPage,
  NotFoundPage,
} from "./views.tsx";
import { archiveMemory, demoteMemory, decideProposal, addNote } from "./writes.ts";
import { buildBriefMarkdown } from "./brief.ts";
import { authenticateWebWrite, establishWebSession, hasWebWriteSession } from "./auth.ts";

const UI_PATH = "/ui";

/** Matches events.triage_state's comment in store/migrations.ts. */
const TRIAGE_STATES = ["pending", "coalescing", "rules_resolved", "llm_resolved", "escalated", "expired", "skipped"];

function readFileSafe(path: string): string | null {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return null;
  }
}

function optionalQuery(value: string | undefined): string | undefined {
  return value && value.length > 0 ? value : undefined;
}

export function createWebUi(deps: DaemonDeps): { path: string; app: Hono } {
  const app = new Hono();
  const db = deps.store.db;

  app.use("*", async (c, next) => {
    if (c.req.method === "GET" || c.req.method === "HEAD" || c.req.path.endsWith("/login")) {
      return next();
    }
    if (authenticateWebWrite(c, deps.config)) return next();
    deps.store.audit("web", "web.write_denied", "path", c.req.path, { reason: "unauthenticated" });
    return c.html('Unauthorized. <a href="/ui/login">Sign in</a>.', 401);
  });

  app.get("/login", (c) => c.html(LoginPage()));

  app.post("/login", async (c) => {
    const body = await c.req.parseBody();
    const token = typeof body.token === "string" ? body.token : "";
    if (!establishWebSession(c, deps.config, token)) {
      deps.store.audit("web", "web.login_denied", "session", "ui", {});
      return c.text("Unauthorized", 401);
    }
    deps.store.audit("web", "web.login", "session", "ui", {});
    return c.redirect(UI_PATH, 303);
  });

  app.get("/", (c) => {
    const filters = {
      vendor: optionalQuery(c.req.query("vendor")),
      severity: optionalQuery(c.req.query("severity")),
      state: optionalQuery(c.req.query("state")),
      repo: optionalQuery(c.req.query("repo")),
      q: optionalQuery(c.req.query("q")),
    };
    const before = optionalQuery(c.req.query("before"));
    const { rows, hasMore } = listEvents(db, { ...filters, before });
    return c.html(
      InboxPage({
        rows,
        hasMore,
        filters,
        vendors: [...Vendor.options],
        severities: [...Severity.options],
        states: TRIAGE_STATES,
      }),
    );
  });

  app.get("/incidents", (c) => {
    const state = optionalQuery(c.req.query("state"));
    const rows = listIncidents(db, state);
    return c.html(IncidentsListPage({ rows, state }));
  });

  app.get("/runs", (c) => {
    const health = getAgentObserverHealth(db);
    const observedAt = health.observed_at ? new Date(health.observed_at).getTime() : Number.NaN;
    const staleAfterMs = Math.max(30_000, deps.config.agentctl_observer.interval_seconds * 3_000);
    const visibleHealth = deps.config.agentctl_observer.enabled && health.state === "ok" &&
      (!Number.isFinite(observedAt) || Date.now() - observedAt > staleAfterMs)
      ? { ...health, state: "stale" as const, error: "The observer has not refreshed recently. Nonterminal states are last-seen evidence, not current liveness." }
      : health;
    const stateValue = optionalQuery(c.req.query("state"));
    const state: AgentRunStateFilter = stateValue === "active" || stateValue === "attention" || stateValue === "finished"
      ? stateValue
      : "all";
    const agent = optionalQuery(c.req.query("agent"));
    const requestedPage = Number.parseInt(c.req.query("page") ?? "0", 10);
    const page = Number.isFinite(requestedPage) && requestedPage > 0 ? requestedPage : 0;
    const observerReliable = deps.config.agentctl_observer.enabled && visibleHealth.state === "ok";
    const list = listAgentRuns(db, { state, agent, page, observerReliable });
    return c.html(RunsPage({
      runs: list.rows,
      health: visibleHealth,
      observerEnabled: deps.config.agentctl_observer.enabled,
      observerReliable,
      summary: getAgentRunSummary(db, observerReliable),
      filters: { state, agent },
      agents: list.agents,
      page: list.page,
      hasNext: list.hasNext,
      filteredTotal: list.total,
      refreshSeconds: deps.config.agentctl_observer.enabled ? deps.config.agentctl_observer.interval_seconds : 0,
      scopeLabel: deps.config.agentctl_observer.observe_all
        ? "All local agentctl runs"
        : `Runs labeled ${deps.config.agentctl_observer.required_labels.join(" + ")}`,
    }));
  });

  app.get("/incidents/:id", (c) => {
    const id = c.req.param("id");
    const chain = getIncidentChain(db, id);
    if (!chain) return c.html(NotFoundPage(`No incident ${id}`), 404);
    return c.html(IncidentDetailPage({ chain }));
  });

  app.get("/memory", (c) => {
    const rules = listRules(db);
    const notes = listNotes(db);
    const pending = listPendingMemories(db);
    const charter = readFileSafe(charterPath(deps.config)) ?? "";
    return c.html(
      MemoryPage({
        rules,
        notes,
        pending,
        charter,
        charterPath: charterPath(deps.config),
        noteAdded: c.req.query("added") === "1",
        canWrite: hasWebWriteSession(c, deps.config),
      }),
    );
  });

  app.post("/memory/:id/archive", (c) => {
    const result = archiveMemory(deps.store, c.req.param("id"));
    if (!result.ok) return c.text(result.reason, 404);
    return c.redirect(`${UI_PATH}/memory`, 303);
  });

  app.post("/memory/:id/demote", (c) => {
    const result = demoteMemory(deps.store, deps.memoryWriter, c.req.param("id"));
    if (!result.ok) return c.text(result.reason, 404);
    return c.redirect(`${UI_PATH}/memory`, 303);
  });

  app.post("/memory/:id/approve", (c) => {
    const result = decideProposal(deps.store, c.req.param("id"), "approve");
    if (!result.ok) return c.text(result.reason, 404);
    return c.redirect(`${UI_PATH}/memory`, 303);
  });

  app.post("/memory/:id/reject", (c) => {
    const result = decideProposal(deps.store, c.req.param("id"), "reject");
    if (!result.ok) return c.text(result.reason, 404);
    return c.redirect(`${UI_PATH}/memory`, 303);
  });

  app.post("/memory/notes", async (c) => {
    const body = await c.req.parseBody();
    const text = typeof body.text === "string" ? body.text.trim() : "";
    if (!text) return c.redirect(`${UI_PATH}/memory`, 303);
    const vendor = typeof body.vendor === "string" ? body.vendor.trim() : "";
    const repo = typeof body.repo === "string" ? body.repo.trim() : "";
    addNote(deps.memoryWriter, text, { vendor: vendor || undefined, repo: repo || undefined });
    return c.redirect(`${UI_PATH}/memory?added=1`, 303);
  });

  app.get("/policy", (c) => {
    const path = policyPath(deps.config);
    const raw = readFileSafe(path);
    let parseOk = true;
    let parseError: string | undefined;
    if (raw !== null) {
      try {
        parseToml(raw);
      } catch (err) {
        parseOk = false;
        parseError = String(err);
      }
    }
    return c.html(PolicyPage({ path, raw, parseOk, parseError }));
  });

  app.get("/digests", (c) => {
    return c.html(DigestsPage({ digests: listDigests(db) }));
  });

  app.get("/brief.md", (c) => {
    return c.text(buildBriefMarkdown(deps.store), 200, { "content-type": "text/markdown; charset=utf-8" });
  });

  return { path: UI_PATH, app };
}
