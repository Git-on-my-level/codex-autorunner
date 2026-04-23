import { REPO_ID, HUB_BASE } from "./env.js";
import { initUiMockFromUrl } from "./uiMock.js";
import {
  api,
  flash,
  repairModalBackgroundIfStuck,
  updateUrlParams,
} from "./utils.js";

let pmaInitialized = false;
let emptyRouteHandled = false;
const PMA_EMPTY_HERO_TEXT =
  "Get started — ask the PM Agent to add your first repo";
const PMA_DEFAULT_HERO_TEXT = "Project Manager";
let hubModulePromise: Promise<typeof import("./hub.js")> | null = null;
let pmaModulePromise: Promise<typeof import("./pma.js")> | null = null;
let notificationsModulePromise: Promise<typeof import("./notifications.js")> | null =
  null;
let repoShellModulesPromise: Promise<{
  archive: typeof import("./archive.js");
  bus: typeof import("./bus.js");
  contextspace: typeof import("./contextspace.js");
  dashboard: typeof import("./dashboard.js");
  health: typeof import("./health.js");
  liveUpdates: typeof import("./liveUpdates.js");
  messages: typeof import("./messages.js");
  mobileCompact: typeof import("./mobileCompact.js");
  settings: typeof import("./settings.js");
  tabs: typeof import("./tabs.js");
  terminal: typeof import("./terminal.js");
  tickets: typeof import("./tickets.js");
}> | null = null;

function loadHubModule(): Promise<typeof import("./hub.js")> {
  hubModulePromise ??= import("./hub.js");
  return hubModulePromise;
}

function loadPMAModule(): Promise<typeof import("./pma.js")> {
  pmaModulePromise ??= import("./pma.js");
  return pmaModulePromise;
}

function loadNotificationsModule(): Promise<typeof import("./notifications.js")> {
  notificationsModulePromise ??= import("./notifications.js");
  return notificationsModulePromise;
}

function loadRepoShellModules() {
  repoShellModulesPromise ??= Promise.all([
    import("./archive.js"),
    import("./bus.js"),
    import("./contextspace.js"),
    import("./dashboard.js"),
    import("./health.js"),
    import("./liveUpdates.js"),
    import("./messages.js"),
    import("./mobileCompact.js"),
    import("./settings.js"),
    import("./tabs.js"),
    import("./terminal.js"),
    import("./tickets.js"),
  ]).then(
    ([
      archive,
      bus,
      contextspace,
      dashboard,
      health,
      liveUpdates,
      messages,
      mobileCompact,
      settings,
      tabs,
      terminal,
      tickets,
    ]) => ({
      archive,
      bus,
      contextspace,
      dashboard,
      health,
      liveUpdates,
      messages,
      mobileCompact,
      settings,
      tabs,
      terminal,
      tickets,
    })
  );
  return repoShellModulesPromise;
}

async function initPMAView(): Promise<void> {
  if (!pmaInitialized) {
    const { initPMA } = await loadPMAModule();
    await initPMA();
    pmaInitialized = true;
  }
}

function setPMARefreshActiveIfLoaded(active: boolean): void {
  if (!pmaInitialized && pmaModulePromise === null) return;
  void loadPMAModule().then(({ setPMARefreshActive }) => {
    setPMARefreshActive(active);
  });
}

function showHubView(): void {
  const hubShell = document.getElementById("hub-shell");
  const pmaShell = document.getElementById("pma-shell");
  if (hubShell) hubShell.classList.remove("hidden");
  if (pmaShell) pmaShell.classList.add("hidden");
  setPMARefreshActiveIfLoaded(false);
  updateModeToggle("manual");
  updateUrlParams({ view: null });
}

function showPMAView(): void {
  const hubShell = document.getElementById("hub-shell");
  const pmaShell = document.getElementById("pma-shell");
  if (hubShell) hubShell.classList.add("hidden");
  if (pmaShell) pmaShell.classList.remove("hidden");
  updateModeToggle("pma");
  void initPMAView().then(() => {
    setPMARefreshActiveIfLoaded(true);
    void loadPMAModule().then(({ drainPendingPrompt }) => {
      drainPendingPrompt();
    });
  });
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
  try {
    await api("/hub/pma/agents", { method: "GET" });
    return true;
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
  const [{ initHub }, { initNotifications }] = await Promise.all([
    loadHubModule(),
    loadNotificationsModule(),
  ]);
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

  const requestedManual = urlParams.get("view") === "manual";
  document.addEventListener("hub:repo-count", (evt) => {
    const detail = (evt as CustomEvent<{ count?: number }>).detail;
    const count = typeof detail?.count === "number" ? detail.count : 0;
    updatePMAHeroForEmptyState(count === 0);
    if (
      !emptyRouteHandled &&
      count === 0 &&
      !requestedManual &&
      !requestedPMA
    ) {
      emptyRouteHandled = true;
      showPMAView();
    } else if (count > 0) {
      emptyRouteHandled = true;
    }
  });

  const { initWalkthrough } = await import("./walkthrough.js");
  initWalkthrough();
}

function updatePMAHeroForEmptyState(empty: boolean): void {
  const heroText = document.querySelector<HTMLElement>(
    "#pma-shell .hub-hero-text"
  );
  const h1 = heroText?.querySelector("h1");
  if (!heroText || !h1) return;
  heroText.classList.toggle("hub-hero-text--empty", empty);
  h1.textContent = empty ? PMA_EMPTY_HERO_TEXT : PMA_DEFAULT_HERO_TEXT;
}

async function initRepoShell(): Promise<void> {
  const {
    archive,
    bus,
    contextspace,
    dashboard,
    health,
    liveUpdates,
    messages,
    mobileCompact,
    settings,
    tabs,
    terminal,
    tickets,
  } = await loadRepoShellModules();
  const { initHealthGate } = health;
  const { initArchive } = archive;
  const { subscribe } = bus;
  const { initContextspace } = contextspace;
  const { initDashboard } = dashboard;
  const { initMessages, initMessageBell } = messages;
  const { initMobileCompact } = mobileCompact;
  const { initRepoSettingsPanel, openRepoSettings } = settings;
  const { initTabs, registerTab, registerHamburgerAction } = tabs;
  const { initTerminal } = terminal;
  const { initTicketFlow } = tickets;
  const { initLiveUpdates } = liveUpdates;

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
  registerTab("contextspace", "Contextspace");
  registerTab("terminal", "Terminal");
  // Menu tabs (shown in hamburger menu)
  registerTab("analytics", "Analytics", { menuTab: true, icon: "📊" });
  registerTab("archive", "Archive", { menuTab: true, icon: "📦" });
  // Settings action in hamburger menu
  registerHamburgerAction("settings", "Settings", "⚙", () => openRepoSettings());

  const initializedTabs = new Set<string>();
  const lazyInit = (tabId: string): void => {
    if (initializedTabs.has(tabId)) return;
    if (tabId === "contextspace") {
      initContextspace();
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

function dismissBootLoader(): void {
  const el = document.getElementById("car-boot-loader");
  if (el) el.remove();
}

function bootstrap() {
  dismissBootLoader();
  initUiMockFromUrl();

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
