import { REPO_ID, HUB_BASE } from "./env.js";
import { initHub } from "./hub.js";
import { initTabs, registerTab, registerHamburgerAction } from "./tabs.js";
import { initTerminal } from "./terminal.js";
import { initTicketFlow } from "./tickets.js";
import { initMessages, initMessageBell } from "./messages.js";
import { initMobileCompact } from "./mobileCompact.js";
import { subscribe } from "./bus.js";
import { initRepoSettingsPanel, openRepoSettings } from "./settings.js";
import {
  flash,
  getAuthToken,
  repairModalBackgroundIfStuck,
  resolvePath,
  updateUrlParams,
} from "./utils.js";
import { initLiveUpdates } from "./liveUpdates.js";
import { initHealthGate } from "./health.js";
import { initWorkspace } from "./workspace.js";
import { initDashboard } from "./dashboard.js";
import { initArchive } from "./archive.js";
import { initPMA } from "./pma.js";
import { initNotifications } from "./notifications.js";

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
  updateModeToggle("manual");
  updateUrlParams({ view: null });
}

function showPMAView(): void {
  const hubShell = document.getElementById("hub-shell");
  const pmaShell = document.getElementById("pma-shell");
  if (hubShell) hubShell.classList.add("hidden");
  if (pmaShell) pmaShell.classList.remove("hidden");
  updateModeToggle("pma");
  void initPMAView();
  updateUrlParams({ view: "pma" });
}

function updateModeToggle(mode: "manual" | "pma"): void {
  const manualBtns = document.querySelectorAll<HTMLButtonElement>(
    '[data-hub-mode="manual"]'
  );
  const pmaBtns = document.querySelectorAll<HTMLButtonElement>(
    '[data-hub-mode="pma"]'
  );
  manualBtns.forEach((btn) => {
    const active = mode === "manual";
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  pmaBtns.forEach((btn) => {
    const active = mode === "pma";
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
}

async function probePMAEnabled(): Promise<boolean> {
  const headers: Record<string, string> = {};
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  try {
    const res = await fetch(resolvePath("/hub/pma/agents"), {
      method: "GET",
      headers,
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function initHubShell(): Promise<void> {
  const hubShell = document.getElementById("hub-shell");
  const repoShell = document.getElementById("repo-shell");
  const manualBtns = Array.from(
    document.querySelectorAll<HTMLButtonElement>('[data-hub-mode="manual"]')
  );
  const pmaBtns = Array.from(
    document.querySelectorAll<HTMLButtonElement>('[data-hub-mode="pma"]')
  );

  if (hubShell) hubShell.classList.remove("hidden");
  if (repoShell) repoShell.classList.add("hidden");
  initHub();
  initNotifications();

  manualBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      showHubView();
    });
  });

  pmaBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      showPMAView();
    });
  });

  const urlParams = new URLSearchParams(window.location.search);
  const requestedPMA = urlParams.get("view") === "pma";
  const pmaEnabled = await probePMAEnabled();

  if (!pmaEnabled) {
    pmaBtns.forEach((btn) => {
      btn.disabled = true;
      btn.setAttribute("aria-disabled", "true");
      btn.title = "Enable PMA in config to use Project Manager";
      btn.classList.add("hidden");
      btn.classList.remove("active");
      btn.setAttribute("aria-selected", "false");
    });
    if (requestedPMA) {
      showHubView();
    }
    return;
  }

  if (requestedPMA) {
    showPMAView();
  }
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
    if (!count && repairModalBackgroundIfStuck()) {
      flash("Recovered from stuck modal state (UI was inert).", "info");
    } else {
      flash(
        count
          ? `UI inert: ${count} modal${count === 1 ? "" : "s"} open`
          : "UI inert but no modal is visible",
        "error"
      );
    }
  }
}

function bootstrap() {
  if (!REPO_ID) {
    void initHubShell();
    return;
  }

  const hubShell = document.getElementById("hub-shell");
  const repoShell = document.getElementById("repo-shell");
  if (repoShell) repoShell.classList.remove("hidden");
  if (hubShell) hubShell.classList.add("hidden");
  void initRepoShell();
}

bootstrap();
