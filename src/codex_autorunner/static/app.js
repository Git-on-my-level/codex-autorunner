// GENERATED FILE - do not edit directly. Source: static_src/
import { REPO_ID, HUB_BASE } from "./env.js";
import { initHub } from "./hub.js";
import { initTabs, registerTab, registerHamburgerAction } from "./tabs.js";
import { initTerminal } from "./terminal.js";
import { initTicketFlow } from "./tickets.js";
import { initMessages, initMessageBell } from "./messages.js";
import { initMobileCompact } from "./mobileCompact.js";
import { subscribe } from "./bus.js";
import { initRepoSettingsPanel, openRepoSettings } from "./settings.js";
import { flash, getAuthToken, repairModalBackgroundIfStuck, resolvePath, updateUrlParams, } from "./utils.js";
import { initLiveUpdates } from "./liveUpdates.js";
import { initHealthGate } from "./health.js";
import { initWorkspace } from "./workspace.js";
import { initDashboard } from "./dashboard.js";
import { initArchive } from "./archive.js";
import { initPMA } from "./pma.js";
let pmaInitialized = false;
async function initPMAView() {
    if (!pmaInitialized) {
        await initPMA();
        pmaInitialized = true;
    }
}
function showHubView() {
    const hubShell = document.getElementById("hub-shell");
    const pmaShell = document.getElementById("pma-shell");
    if (hubShell)
        hubShell.classList.remove("hidden");
    if (pmaShell)
        pmaShell.classList.add("hidden");
    updateUrlParams({ view: null });
}
function showPMAView() {
    const hubShell = document.getElementById("hub-shell");
    const pmaShell = document.getElementById("pma-shell");
    if (hubShell)
        hubShell.classList.add("hidden");
    if (pmaShell)
        pmaShell.classList.remove("hidden");
    void initPMAView();
    updateUrlParams({ view: "pma" });
}
async function probePMAEnabled() {
    const headers = {};
    const token = getAuthToken();
    if (token) {
        headers.Authorization = `Bearer ${token}`;
    }
    try {
        const res = await fetch(resolvePath("/hub/pma/agents"), {
            method: "GET",
            headers,
        });
        if (res.ok)
            return true;
        if (res.status === 403 || res.status === 404)
            return false;
        return true;
    }
    catch {
        return true;
    }
}
async function initHubShell() {
    const hubShell = document.getElementById("hub-shell");
    const repoShell = document.getElementById("repo-shell");
    const hubPmaBtn = document.getElementById("hub-pma");
    const pmaBackBtn = document.getElementById("pma-back");
    if (hubShell)
        hubShell.classList.remove("hidden");
    if (repoShell)
        repoShell.classList.add("hidden");
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
    const urlParams = new URLSearchParams(window.location.search);
    const requestedPMA = urlParams.get("view") === "pma";
    const pmaEnabled = await probePMAEnabled();
    if (!pmaEnabled) {
        if (hubPmaBtn) {
            hubPmaBtn.classList.add("hidden");
            hubPmaBtn.setAttribute("aria-hidden", "true");
            hubPmaBtn.disabled = true;
        }
        if (requestedPMA) {
            showHubView();
        }
        return;
    }
    if (requestedPMA) {
        showPMAView();
    }
}
async function initRepoShell() {
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
    const initializedTabs = new Set();
    const lazyInit = (tabId) => {
        if (initializedTabs.has(tabId))
            return;
        if (tabId === "workspace") {
            initWorkspace();
        }
        else if (tabId === "inbox" || tabId === "messages") {
            initMessages();
        }
        else if (tabId === "analytics") {
            initDashboard();
        }
        else if (tabId === "archive") {
            initArchive();
        }
        else if (tabId === "tickets") {
            initTicketFlow();
        }
        initializedTabs.add(tabId);
    };
    subscribe("tab:change", (tabId) => {
        if (tabId === "terminal") {
            initTerminal();
        }
        lazyInit(tabId);
    });
    initTabs(defaultTab);
    const activePanel = document.querySelector(".panel.active");
    if (activePanel?.id) {
        lazyInit(activePanel.id);
    }
    const terminalPanel = document.getElementById("terminal");
    terminalPanel?.addEventListener("pointerdown", () => {
        lazyInit("terminal");
    }, { once: true });
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
        }
        else {
            flash(count
                ? `UI inert: ${count} modal${count === 1 ? "" : "s"} open`
                : "UI inert but no modal is visible", "error");
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
    if (repoShell)
        repoShell.classList.remove("hidden");
    if (hubShell)
        hubShell.classList.add("hidden");
    void initRepoShell();
}
bootstrap();
