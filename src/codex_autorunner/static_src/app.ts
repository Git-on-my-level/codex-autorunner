import { REPO_ID, HUB_BASE } from "./env.js";
import { initHub } from "./hub.js";
import { initTabs, registerTab, registerHamburgerAction } from "./tabs.js";
import { initTerminal } from "./terminal.js";
import { initTicketFlow } from "./tickets.js";
import { initMessages, initMessageBell } from "./messages.js";
import { initMobileCompact } from "./mobileCompact.js";
import { subscribe } from "./bus.js";
import { initRepoSettingsPanel, openRepoSettings } from "./settings.js";
import { flash } from "./utils.js";
import { initLiveUpdates } from "./liveUpdates.js";
import { initHealthGate } from "./health.js";
import { initWorkspace } from "./workspace.js";
import { initDashboard } from "./dashboard.js";
import { initArchive } from "./archive.js";
import { initPMA } from "./pma.js";

let pmaInitialized = false;

async function initPMAView(): Promise<void> {
  if (!pmaInitialized) {
    await initPMA();
    pmaInitialized = true;
  }
}

function showHubView(): void {
  const hubShell = document.getElementById("hub-shell");
  const pmaShell = document.getElementById("pma-shell");
  if (hubShell) hubShell.classList.remove("hidden");
  if (pmaShell) pmaShell.classList.add("hidden");
}

function showPMAView(): void {
  const hubShell = document.getElementById("hub-shell");
  const pmaShell = document.getElementById("pma-shell");
  if (hubShell) hubShell.classList.add("hidden");
  if (pmaShell) pmaShell.classList.remove("hidden");
  void initPMAView();
}

async function initRepoShell(): Promise<void> {
  await initHealthGate();

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
  registerTab("inbox", "Inbox");
  registerTab("workspace", "Workspace");
  registerTab("terminal", "Terminal");
  // Menu tabs (shown in hamburger menu)
  registerTab("analytics", "Analytics", { menuTab: true, icon: "📊" });
  registerTab("archive", "Archive", { menuTab: true, icon: "📦" });
  // Settings action in hamburger menu
  registerHamburgerAction("settings", "Settings", "⚙", () => openRepoSettings());

  const initializedTabs = new Set<string>();
  const lazyInit = (tabId: string): void => {
    if (initializedTabs.has(tabId)) return;
    if (tabId === "workspace") {
      initWorkspace();
    } else if (tabId === "inbox" || tabId === "messages") {
      initMessages();
    } else if (tabId === "analytics") {
      initDashboard();
    } else if (tabId === "archive") {
      initArchive();
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
  const hubPmaBtn = document.getElementById("hub-pma") as HTMLButtonElement | null;
  const pmaBackBtn = document.getElementById("pma-back") as HTMLButtonElement | null;

  if (!REPO_ID) {
    if (hubShell) hubShell.classList.remove("hidden");
    if (repoShell) repoShell.classList.add("hidden");
    initHub();

    if (hubPmaBtn) {
      hubPmaBtn.addEventListener("click", () => {
        showPMAView();
      });
    }

    if (pmaBackBtn) {
      pmaBackBtn.addEventListener("click", () => {
        showHubView();
      });
    }

    // Check URL params for PMA view
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get("view") === "pma") {
      showPMAView();
    }
    return;
  }

  if (repoShell) repoShell.classList.remove("hidden");
  if (hubShell) hubShell.classList.add("hidden");
  void initRepoShell();
}

bootstrap();
