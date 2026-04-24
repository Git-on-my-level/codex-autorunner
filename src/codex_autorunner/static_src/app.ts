import { REPO_ID, HUB_BASE } from "./env.js";
import { importVersionedModule } from "./assetLoader.js";
import { initUiMockFromUrl } from "./uiMock.js";
import {
  consumeOnboardingUrlReset,
  scheduleOnboardingPromptIfFirstRun,
} from "./walkthrough.js";
import {
  api,
  flash,
  repairModalBackgroundIfStuck,
  updateUrlParams,
} from "./utils.js";

let pmaInitialized = false;
let emptyRouteHandled = false;
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
  hubModulePromise ??= importVersionedModule<typeof import("./hub.js")>(
    "./hub.js"
  );
  return hubModulePromise;
}

function loadPMAModule(): Promise<typeof import("./pma.js")> {
  pmaModulePromise ??= importVersionedModule<typeof import("./pma.js")>(
    "./pma.js"
  );
  return pmaModulePromise;
}

function loadNotificationsModule(): Promise<typeof import("./notifications.js")> {
  notificationsModulePromise ??= importVersionedModule<
    typeof import("./notifications.js")
  >("./notifications.js");
  return notificationsModulePromise;
}

function loadRepoShellModules() {
  repoShellModulesPromise ??= Promise.all([
    importVersionedModule<typeof import("./archive.js")>("./archive.js"),
    importVersionedModule<typeof import("./bus.js")>("./bus.js"),
    importVersionedModule<typeof import("./contextspace.js")>(
      "./contextspace.js"
    ),
    importVersionedModule<typeof import("./dashboard.js")>("./dashboard.js"),
    importVersionedModule<typeof import("./health.js")>("./health.js"),
    importVersionedModule<typeof import("./liveUpdates.js")>(
      "./liveUpdates.js"
    ),
    importVersionedModule<typeof import("./messages.js")>("./messages.js"),
    importVersionedModule<typeof import("./mobileCompact.js")>(
      "./mobileCompact.js"
    ),
    importVersionedModule<typeof import("./settings.js")>("./settings.js"),
    importVersionedModule<typeof import("./tabs.js")>("./tabs.js"),
    importVersionedModule<typeof import("./terminal.js")>("./terminal.js"),
    importVersionedModule<typeof import("./tickets.js")>("./tickets.js"),
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
    void loadPMAModule().then((mod) => {
      if (typeof mod.drainPendingPrompt === "function") {
        mod.drainPendingPrompt();
      }
    });
  });
  updateUrlParams({ view: "pma" });
}

function applyScheduledOnboardingPrompt(): void {
  void loadPMAModule().then((mod) => {
    if (typeof mod.drainPendingPrompt === "function") {
      mod.drainPendingPrompt();
    }
  });
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

interface PMAStatus {
  enabled: boolean;
  hasAgents: boolean;
}

async function probePMAEnabled(): Promise<PMAStatus> {
  try {
    const data = await api("/hub/pma/agents", { method: "GET" });
    const agents = (data as { agents?: unknown })?.agents;
    const hasAgents = Array.isArray(agents) && agents.length > 0;
    return { enabled: true, hasAgents };
  } catch {
    return { enabled: false, hasAgents: false };
  }
}

async function initHubShell(): Promise<void> {
  consumeOnboardingUrlReset();
  const hubShell = document.getElementById("hub-shell");
  const repoShell = document.getElementById("repo-shell");
  const manualBtns = Array.from(
    document.querySelectorAll<HTMLButtonElement>('[data-hub-mode="manual"]')
  );
  const pmaBtns = Array.from(
    document.querySelectorAll<HTMLButtonElement>('[data-hub-mode="pma"]')
  );
  let latestRepoCount: number | null = null;
  let pmaStatusResolved = false;
  let requestedPMA = false;
  let requestedManual = false;
  let hasAgents = false;

  const handleRepoCount = (count: number): void => {
    latestRepoCount = count;
    if (!pmaStatusResolved) return;

    const isEmptyHub = count === 0;
    const onboardingEligible = isEmptyHub && !requestedManual && hasAgents;
    if (onboardingEligible) {
      const scheduled = scheduleOnboardingPromptIfFirstRun();
      if (scheduled && requestedPMA) {
        applyScheduledOnboardingPrompt();
      }
    }
    if (
      !emptyRouteHandled &&
      isEmptyHub &&
      !requestedManual &&
      !requestedPMA
    ) {
      emptyRouteHandled = true;
      showPMAView();
    } else if (count > 0 || (requestedPMA && isEmptyHub)) {
      emptyRouteHandled = true;
    }
  };

  document.addEventListener("hub:repo-count", (evt) => {
    const detail = (evt as CustomEvent<{ count?: number }>).detail;
    const count = typeof detail?.count === "number" ? detail.count : 0;
    handleRepoCount(count);
  });

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
  requestedPMA = urlParams.get("view") === "pma";
  requestedManual = urlParams.get("view") === "manual";
  const { enabled: pmaEnabled, hasAgents: resolvedHasAgents } =
    await probePMAEnabled();
  hasAgents = resolvedHasAgents;
  pmaStatusResolved = true;

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

  setNoAgentsNoticeVisible(!hasAgents);

  if (requestedPMA) {
    showPMAView();
  }

  if (!hasAgents && !requestedPMA) {
    // No supported agent installed — PMA chat can't run, just route so the user
    // sees the install notice instead of an empty hub.
    emptyRouteHandled = true;
    showPMAView();
  }
  if (latestRepoCount !== null) {
    handleRepoCount(latestRepoCount);
  }
}

function setNoAgentsNoticeVisible(visible: boolean): void {
  const notice = document.getElementById("pma-no-agents-notice");
  if (notice) notice.classList.toggle("hidden", !visible);
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
