/**
 * WS-F owns src/surfaces/web/: server-rendered JSX + htmx power-user UI
 * (inbox, incident detail, memory browser/editor, policy, digest archive)
 * plus GET /brief.md for other agents to curl.
 * Scaffold stub: /brief.md with open-escalation counts.
 */
import { Hono } from "hono";
import type { DaemonDeps } from "../../ports.ts";

export function createWebUi(deps: DaemonDeps): { path: string; app: Hono } {
  const app = new Hono();

  app.get("/", (c) => c.text("CAR v3 — web UI pending (see /brief.md)"));

  app.get("/brief.md", (c) => {
    const open = deps.store.db
      .query("SELECT COUNT(*) n FROM escalations WHERE state = 'pending'")
      .get() as { n: number };
    const sessions = deps.store.db
      .query("SELECT COUNT(*) n FROM sessions WHERE state = 'active'")
      .get() as { n: number };
    const md = [
      "# CAR brief",
      "",
      `- Open escalations: ${open.n}`,
      `- Active sessions: ${sessions.n}`,
    ].join("\n");
    return c.text(md, 200, { "content-type": "text/markdown; charset=utf-8" });
  });

  return { path: "/ui", app };
}
