from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TICKETS_JS = (
    _REPO_ROOT / "src" / "codex_autorunner" / "static" / "generated" / "tickets.js"
)


def _node_package_dir(package_name: str) -> Path:
    direct = _REPO_ROOT / "node_modules" / package_name / "package.json"
    if direct.exists():
        return direct.parent

    pnpm_matches = sorted(
        (_REPO_ROOT / "node_modules" / ".pnpm").glob(
            f"{package_name}@*/node_modules/{package_name}/package.json"
        )
    )
    if pnpm_matches:
        return pnpm_matches[-1].parent

    pytest.skip(
        f"Node package {package_name!r} is not installed; run pnpm install first"
    )


def _run_ticket_flow_node_script(markup: str, actions: str) -> None:
    tickets_js = _tickets_js_path()
    jsdom_dir = _node_package_dir("jsdom")
    script = textwrap.dedent(f"""
        import assert from "node:assert/strict";
        import {{ createRequire }} from "node:module";
        import {{ pathToFileURL }} from "node:url";

        const require = createRequire(import.meta.url);
        const {{ JSDOM }} = require("{jsdom_dir.as_posix()}");
        const ticketsJsUrl = pathToFileURL("{tickets_js.as_posix()}").href;
        const dom = new JSDOM(
          `<!doctype html><html><body>
            {markup}
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

        {actions}
        """)
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        cwd=str(_REPO_ROOT),
    )


def _tickets_js_path() -> Path:
    if _TICKETS_JS.exists():
        return _TICKETS_JS

    pytest.skip(
        "legacy generated ticket-flow asset is not present at "
        f"{_TICKETS_JS}; build or install generated static assets before "
        "running this legacy integration test"
    )


def test_ticket_flow_compact_live_output_falls_back_to_stream_deltas() -> None:
    markup = textwrap.dedent("""
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
        """)
    actions = textwrap.dedent("""
        const mod = await import(ticketsJsUrl);
        const helpers = mod.__ticketFlowTest;

        helpers.clearLiveOutput();
        helpers.initLiveOutputPanel();
        helpers.setFlowStartedAt(Date.parse("2026-04-12T00:00:00Z"));

        helpers.handleFlowEvent({
          event_type: "agent_stream_delta",
          timestamp: "2026-04-12T00:00:01Z",
          data: { delta: "This is live " },
        });
        helpers.handleFlowEvent({
          event_type: "agent_stream_delta",
          timestamp: "2026-04-12T00:00:02Z",
          data: { delta: "codex output" },
        });

        await new Promise((resolve) => setTimeout(resolve, 20));

        const detail = document.getElementById("ticket-live-output-text")?.textContent || "";
        const status = document.getElementById("ticket-live-output-status")?.textContent || "";

        assert.match(detail, /This is live codex output/);
        assert.equal(status, "Streaming");
        """)

    _run_ticket_flow_node_script(markup, actions)


def test_ticket_flow_live_output_expands_from_collapsed_bar() -> None:
    markup = textwrap.dedent("""
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
        """)
    actions = textwrap.dedent("""
        const mod = await import(ticketsJsUrl);
        const helpers = mod.__ticketFlowTest;

        helpers.clearLiveOutput();
        helpers.initLiveOutputPanel();
        helpers.setFlowStartedAt(Date.parse("2026-04-12T00:00:00Z"));

        helpers.handleFlowEvent({
          event_type: "agent_stream_delta",
          timestamp: "2026-04-12T00:00:01Z",
          data: { delta: "This is live " },
        });
        helpers.handleFlowEvent({
          event_type: "agent_stream_delta",
          timestamp: "2026-04-12T00:00:02Z",
          data: { delta: "codex output" },
        });

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

        panelToggle?.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));

        assert.equal(panelToggle?.getAttribute("aria-expanded"), "true");
        assert.equal(detailWrapper?.classList.contains("hidden"), false);
        assert.equal(compactWrapper?.classList.contains("hidden"), true);
        """)

    _run_ticket_flow_node_script(markup, actions)


def test_ticket_flow_compact_live_output_shows_step_progress_when_no_agent_text() -> (
    None
):
    markup = textwrap.dedent("""
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
        """)
    actions = textwrap.dedent("""
        const mod = await import(ticketsJsUrl);
        const helpers = mod.__ticketFlowTest;

        helpers.clearLiveOutput();
        helpers.initLiveOutputPanel();
        helpers.setFlowStartedAt(Date.parse("2026-04-12T00:00:00Z"));

        helpers.handleFlowEvent({
          event_type: "step_started",
          timestamp: "2026-04-12T00:00:01Z",
          data: { step_name: "ticket_turn" },
        });

        await new Promise((resolve) => setTimeout(resolve, 20));

        const detail = document.getElementById("ticket-live-output-text")?.textContent || "";

        assert.match(detail, /--- Step: ticket_turn ---/);
        """)

    _run_ticket_flow_node_script(markup, actions)
