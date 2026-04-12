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
            <div id="ticket-live-output-status"></div>
            <button id="ticket-live-output-view-toggle" type="button"></button>
            <div id="ticket-live-output-compact"></div>
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

        const compact = document.getElementById("ticket-live-output-compact")?.textContent || "";
        const detail = document.getElementById("ticket-live-output-text")?.textContent || "";
        const status = document.getElementById("ticket-live-output-status")?.textContent || "";

        assert.match(detail, /This is live codex output/);
        assert.match(compact, /This is live codex output/);
        assert.doesNotMatch(compact, /Waiting for agent output/);
        assert.equal(status, "Streaming");
        """
    )

    subprocess.run(["node", "--input-type=module", "-e", script], check=True)
