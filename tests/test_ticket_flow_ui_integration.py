from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_ticket_flow_compact_live_output_falls_back_to_stream_deltas() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tickets_js = (
        repo_root / "src" / "codex_autorunner" / "static" / "generated" / "tickets.js"
    )

    script = textwrap.dedent(
        f"""
        import assert from "node:assert/strict";
        import {{ pathToFileURL }} from "node:url";
        import {{ JSDOM }} from "jsdom";

        const dom = new JSDOM(
          `<!doctype html><html><body>
            <div id="ticket-live-output-panel" class="ticket-live-output-panel"></div>
            <div id="ticket-live-output-status"></div>
            <button id="ticket-live-output-panel-toggle" type="button"></button>
            <pre id="ticket-live-output-compact"></pre>
            <div id="ticket-live-output-detail" class="hidden"></div>
            <pre id="ticket-live-output-text"></pre>
            <div id="ticket-live-output-events" class="hidden">
              <span id="ticket-live-output-events-count"></span>
              <div id="ticket-live-output-events-list"></div>
            </div>
          </body></html>`,
          {{ url: "http://localhost/repos/test/" }}
        );

        globalThis.window = dom.window;
        globalThis.document = dom.window.document;
        globalThis.HTMLElement = dom.window.HTMLElement;
        globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
        globalThis.Node = dom.window.Node;
        globalThis.Event = dom.window.Event;
        globalThis.CustomEvent = dom.window.CustomEvent;
        globalThis.DOMParser = dom.window.DOMParser;
        globalThis.localStorage = dom.window.localStorage;
        globalThis.sessionStorage = dom.window.sessionStorage;
        globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
        globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
        globalThis.fetch = async () => {{
          throw new Error("unexpected fetch in ticket flow integration test");
        }};

        const moduleUrl = pathToFileURL("{tickets_js.as_posix()}").href;
        const mod = await import(moduleUrl);
        const helpers = mod.__ticketFlowTest;

        helpers.clearLiveOutput();
        helpers.initLiveOutputPanel();
        helpers.setFlowStartedAt(Date.parse("2026-04-12T00:00:00Z"));

        helpers.handleFlowEvent({{
          event_type: "agent_stream_delta",
          timestamp: "2026-04-12T00:00:01Z",
          data: {{ delta: "This is live " }},
        }});
        helpers.handleFlowEvent({{
          event_type: "agent_stream_delta",
          timestamp: "2026-04-12T00:00:02Z",
          data: {{ delta: "codex output" }},
        }});

        await new Promise((resolve) => setTimeout(resolve, 20));

        const detail = document.getElementById("ticket-live-output-text")?.textContent || "";
        const status = document.getElementById("ticket-live-output-status")?.textContent || "";

        assert.match(detail, /This is live codex output/);
        assert.equal(status, "Streaming");
        """
    )

    subprocess.run(["node", "--input-type=module", "-e", script], check=True)


def test_ticket_flow_live_output_expands_from_collapsed_bar() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tickets_js = (
        repo_root / "src" / "codex_autorunner" / "static" / "generated" / "tickets.js"
    )

    script = textwrap.dedent(
        f"""
        import assert from "node:assert/strict";
        import {{ pathToFileURL }} from "node:url";
        import {{ JSDOM }} from "jsdom";

        const dom = new JSDOM(
          `<!doctype html><html><body>
            <div id="ticket-live-output-panel" class="ticket-live-output-panel collapsed"></div>
            <div id="ticket-live-output-status"></div>
            <button id="ticket-live-output-panel-toggle" type="button" aria-expanded="false"></button>
            <span id="ticket-live-output-chevron"></span>
            <pre id="ticket-live-output-compact"></pre>
            <div id="ticket-live-output-detail" class="hidden"></div>
            <pre id="ticket-live-output-text"></pre>
            <div id="ticket-live-output-events" class="hidden">
              <span id="ticket-live-output-events-count"></span>
              <div id="ticket-live-output-events-list"></div>
            </div>
          </body></html>`,
          {{ url: "http://localhost/repos/test/" }}
        );

        globalThis.window = dom.window;
        globalThis.document = dom.window.document;
        globalThis.HTMLElement = dom.window.HTMLElement;
        globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
        globalThis.Node = dom.window.Node;
        globalThis.Event = dom.window.Event;
        globalThis.CustomEvent = dom.window.CustomEvent;
        globalThis.DOMParser = dom.window.DOMParser;
        globalThis.localStorage = dom.window.localStorage;
        globalThis.sessionStorage = dom.window.sessionStorage;
        globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
        globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
        globalThis.fetch = async () => {{
          throw new Error("unexpected fetch in ticket flow integration test");
        }};

        const moduleUrl = pathToFileURL("{tickets_js.as_posix()}").href;
        const mod = await import(moduleUrl);
        const helpers = mod.__ticketFlowTest;

        helpers.clearLiveOutput();
        helpers.initLiveOutputPanel();
        helpers.setFlowStartedAt(Date.parse("2026-04-12T00:00:00Z"));

        helpers.handleFlowEvent({{
          event_type: "agent_stream_delta",
          timestamp: "2026-04-12T00:00:01Z",
          data: {{ delta: "This is live " }},
        }});
        helpers.handleFlowEvent({{
          event_type: "agent_stream_delta",
          timestamp: "2026-04-12T00:00:02Z",
          data: {{ delta: "codex output" }},
        }});

        await new Promise((resolve) => setTimeout(resolve, 20));

        const detailWrapper = document.getElementById("ticket-live-output-detail");
        const compactWrapper = document.getElementById("ticket-live-output-compact");
        const detail = document.getElementById("ticket-live-output-text")?.textContent || "";
        const status = document.getElementById("ticket-live-output-status")?.textContent || "";
        const panelToggle = document.getElementById("ticket-live-output-panel-toggle");

        assert.equal(detailWrapper?.classList.contains("hidden"), true);
        assert.equal(compactWrapper?.classList.contains("hidden"), true);
        assert.match(detail, /This is live codex output/);
        assert.equal(status, "Streaming");

        panelToggle?.dispatchEvent(new dom.window.MouseEvent("click", {{ bubbles: true }}));

        assert.equal(panelToggle?.getAttribute("aria-expanded"), "true");
        assert.equal(detailWrapper?.classList.contains("hidden"), false);
        assert.equal(compactWrapper?.classList.contains("hidden"), true);
        """
    )

    subprocess.run(["node", "--input-type=module", "-e", script], check=True)


def test_ticket_flow_compact_live_output_shows_step_progress_when_no_agent_text() -> (
    None
):
    repo_root = Path(__file__).resolve().parents[1]
    tickets_js = (
        repo_root / "src" / "codex_autorunner" / "static" / "generated" / "tickets.js"
    )

    script = textwrap.dedent(
        f"""
        import assert from "node:assert/strict";
        import {{ pathToFileURL }} from "node:url";
        import {{ JSDOM }} from "jsdom";

        const dom = new JSDOM(
          `<!doctype html><html><body>
            <div id="ticket-live-output-panel" class="ticket-live-output-panel"></div>
            <div id="ticket-live-output-status"></div>
            <button id="ticket-live-output-panel-toggle" type="button"></button>
            <pre id="ticket-live-output-compact"></pre>
            <div id="ticket-live-output-detail" class="hidden"></div>
            <pre id="ticket-live-output-text"></pre>
            <div id="ticket-live-output-events" class="hidden">
              <span id="ticket-live-output-events-count"></span>
              <div id="ticket-live-output-events-list"></div>
            </div>
          </body></html>`,
          {{ url: "http://localhost/repos/test/" }}
        );

        globalThis.window = dom.window;
        globalThis.document = dom.window.document;
        globalThis.HTMLElement = dom.window.HTMLElement;
        globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
        globalThis.Node = dom.window.Node;
        globalThis.Event = dom.window.Event;
        globalThis.CustomEvent = dom.window.CustomEvent;
        globalThis.DOMParser = dom.window.DOMParser;
        globalThis.localStorage = dom.window.localStorage;
        globalThis.sessionStorage = dom.window.sessionStorage;
        globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
        globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
        globalThis.fetch = async () => {{
          throw new Error("unexpected fetch in ticket flow integration test");
        }};

        const moduleUrl = pathToFileURL("{tickets_js.as_posix()}").href;
        const mod = await import(moduleUrl);
        const helpers = mod.__ticketFlowTest;

        helpers.clearLiveOutput();
        helpers.initLiveOutputPanel();
        helpers.setFlowStartedAt(Date.parse("2026-04-12T00:00:00Z"));

        helpers.handleFlowEvent({{
          event_type: "step_started",
          timestamp: "2026-04-12T00:00:01Z",
          data: {{ step_name: "ticket_turn" }},
        }});

        await new Promise((resolve) => setTimeout(resolve, 20));

        const detail = document.getElementById("ticket-live-output-text")?.textContent || "";

        assert.match(detail, /--- Step: ticket_turn ---/);
        """
    )

    subprocess.run(["node", "--input-type=module", "-e", script], check=True)
