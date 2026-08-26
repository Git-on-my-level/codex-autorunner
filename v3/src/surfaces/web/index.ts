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
} from "./queries.ts";
import {
  InboxPage,
  IncidentsListPage,
  IncidentDetailPage,
  MemoryPage,
  PolicyPage,
  DigestsPage,
  NotFoundPage,
} from "./views.tsx";
import { archiveMemory, demoteMemory, decideProposal, addNote } from "./writes.ts";
import { buildBriefMarkdown } from "./brief.ts";

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
