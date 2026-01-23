import { REPO_ID, HUB_BASE } from "./env.js";
import { initHub } from "./hub.js";
import { initTabs, registerTab } from "./tabs.js";
import { initDocs } from "./docs.js";
import { initTerminal } from "./terminal.js";
import { initTicketFlow } from "./tickets.js";
import { initMessages, initMessageBell } from "./messages.js";
import { initMobileCompact } from "./mobileCompact.js";
import { subscribe } from "./bus.js";
import { initRepoSettingsPanel } from "./settings.js";
import { flash } from "./utils.js";
import { initLiveUpdates } from "./liveUpdates.js";
import { initHealthGate } from "./health.js";

function disableLegacyAnalyticsUI(): void {
  // Ticket-first: these panels and their API calls are deprecated.
  const legacyIds = [
    "runner-controls",
    "analytics-runs",
    "analytics-logs",
  ];
  for (const id of legacyIds) {
    const el = document.getElementById(id);
    if (el) el.classList.add("hidden");
  }

  const panel = document.getElementById("analytics");
  if (!panel) return;
  if (document.getElementById("ticket-first-analytics-note")) return;

  const note = document.createElement("div");
  note.id = "ticket-first-analytics-note";
  note.className = "status-card";
  const title = document.createElement("h3");
  title.textContent = "Ticket-first mode";
  const body = document.createElement("p");
  body.textContent =
    "Legacy autorunner / GitHub / PR flow panels have been disabled. Analytics will be rebuilt around ticket_flow.";
  note.append(title, body);
  panel.insertBefore(note, panel.firstChild);
}

async function initRepoShell(): Promise<void> {
  await initHealthGate();
  disableLegacyAnalyticsUI();

  if (REPO_ID) {
    const navBar = document.querySelector(".nav-bar");
    if (navBar) {
      const backBtn = document.createElement("a");
      backBtn.href = HUB_BASE || "/";
      backBtn.className = "hub-back-btn";
      backBtn.textContent = "← Hub";
      backBtn.title = "Back to Hub";
      navBar.insertBefore(backBtn, navBar.firstChild);
    }
    const brand = document.querySelector(".nav-brand");
  if (brand) {
      const repoName = document.createElement("span");
      repoName.className = "nav-repo-name";
      repoName.textContent = REPO_ID;
      brand.insertAdjacentElement("afterend", repoName);
    }
  }

  const defaultTab = REPO_ID ? "tickets" : "analytics";

  registerTab("tickets", "Tickets");
  registerTab("messages", "Inbox");
  registerTab("analytics", "Analytics");
  registerTab("docs", "Docs");
  registerTab("terminal", "Terminal");

  const initializedTabs = new Set<string>();
  const lazyInit = (tabId: string): void => {
    if (initializedTabs.has(tabId)) return;
    if (tabId === "docs") {
      initDocs();
    } else if (tabId === "messages") {
      initMessages();
    } else if (tabId === "analytics") {
      // Ticket-first: keep Analytics as a stub panel for now and avoid all legacy
      // dashboard / GitHub / PR flow boot paths.
      disableLegacyAnalyticsUI();
    } else if (tabId === "tickets") {
      initTicketFlow();
    }
    initializedTabs.add(tabId);
  };

  subscribe("tab:change", (tabId: unknown) => {
    if (tabId === "terminal") {
      initTerminal();
    }
    lazyInit(tabId as string);
  });

  initTabs(defaultTab);
  const activePanel = document.querySelector(".panel.active") as HTMLElement;
  if (activePanel?.id) {
    lazyInit(activePanel.id);
  }
  const terminalPanel = document.getElementById("terminal");
  terminalPanel?.addEventListener(
    "pointerdown",
    () => {
      lazyInit("terminal");
    },
    { once: true }
  );
  initMessageBell();
  initLiveUpdates();
  initRepoSettingsPanel();
  initMobileCompact();

  const repoShell = document.getElementById("repo-shell");
  if (repoShell?.hasAttribute("inert")) {
    const openModals = document.querySelectorAll(".modal-overlay:not([hidden])");
    const count = openModals.length;
    flash(
      count
        ? `UI inert: ${count} modal${count === 1 ? "" : "s"} open`
        : "UI inert but no modal is visible",
      "error"
    );
  }
}

function bootstrap() {
  const hubShell = document.getElementById("hub-shell");
  const repoShell = document.getElementById("repo-shell");

  if (!REPO_ID) {
    if (hubShell) hubShell.classList.remove("hidden");
    if (repoShell) repoShell.classList.add("hidden");
    initHub();
    return;
  }

  if (repoShell) repoShell.classList.remove("hidden");
  if (hubShell) hubShell.classList.add("hidden");
  void initRepoShell();
}

bootstrap();
