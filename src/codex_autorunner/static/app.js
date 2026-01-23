import { REPO_ID, HUB_BASE } from "./env.js";
import { initHub } from "./hub.js";
import { initTabs, registerTab } from "./tabs.js";
import { initDashboard } from "./dashboard.js";
import { initDocs } from "./docs.js";
import { initLogs } from "./logs.js";
import { initTerminal } from "./terminal.js";
import { initRuns } from "./runs.js";
import { initTicketFlow } from "./tickets.js";
import { initMessages, initMessageBell } from "./messages.js";
import { loadState } from "./state.js";
import { initGitHub } from "./github.js";
import { initMobileCompact } from "./mobileCompact.js";
import { subscribe } from "./bus.js";
import { initRepoSettingsPanel } from "./settings.js";
import { flash } from "./utils.js";
import { initLiveUpdates } from "./liveUpdates.js";
function initRepoShell() {
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
    registerTab("dashboard", "Dashboard");
    registerTab("messages", "Messages", { hidden: true });
    registerTab("docs", "Docs");
    registerTab("runs", "Runs");
    registerTab("tickets", "Tickets");
    registerTab("logs", "Logs");
    registerTab("terminal", "Terminal");
    const initializedTabs = new Set();
    const lazyInit = (tabId) => {
        if (initializedTabs.has(tabId))
            return;
        if (tabId === "docs") {
            initDocs();
        }
        else if (tabId === "messages") {
            initMessages();
        }
        else if (tabId === "logs") {
            initLogs();
        }
        else if (tabId === "tickets") {
            initTicketFlow();
        }
        else if (tabId === "runs") {
            initRuns();
        }
        initializedTabs.add(tabId);
    };
    subscribe("tab:change", (tabId) => {
        if (tabId === "terminal") {
            initTerminal();
        }
        lazyInit(tabId);
    });
    initTabs();
    const activePanel = document.querySelector(".panel.active");
    if (activePanel?.id) {
        lazyInit(activePanel.id);
    }
    const terminalPanel = document.getElementById("terminal");
    terminalPanel?.addEventListener("pointerdown", () => {
        lazyInit("terminal");
    }, { once: true });
    initDashboard();
    initMessageBell();
    initLiveUpdates();
    initRepoSettingsPanel();
    initGitHub();
    initMobileCompact();
    loadState();
    const repoShell = document.getElementById("repo-shell");
    if (repoShell?.hasAttribute("inert")) {
        const openModals = document.querySelectorAll(".modal-overlay:not([hidden])");
        const count = openModals.length;
        flash(count
            ? `UI inert: ${count} modal${count === 1 ? "" : "s"} open`
            : "UI inert but no modal is visible", "error");
    }
}
function bootstrap() {
    const hubShell = document.getElementById("hub-shell");
    const repoShell = document.getElementById("repo-shell");
    if (!REPO_ID) {
        if (hubShell)
            hubShell.classList.remove("hidden");
        if (repoShell)
            repoShell.classList.add("hidden");
        initHub();
        return;
    }
    if (repoShell)
        repoShell.classList.remove("hidden");
    if (hubShell)
        hubShell.classList.add("hidden");
    initRepoShell();
}
bootstrap();
