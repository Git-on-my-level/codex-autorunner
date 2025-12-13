import { detectContext, REPO_ID, HUB_BASE } from "./env.js";
import { initHub } from "./hub.js";
import { initTabs, registerTab } from "./tabs.js";
import { initDashboard } from "./dashboard.js";
import { initDocs } from "./docs.js";
import { initLogs } from "./logs.js";
import { initTerminal } from "./terminal.js";
import { loadState } from "./state.js";
import { initGitHub } from "./github.js";

function initRepoShell() {
  // If this is a repo under a hub, show back button and repo name
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
    // Add repo name after the CAR brand (keep CAR visible)
    const brand = document.querySelector(".nav-brand");
    if (brand) {
      const repoName = document.createElement("span");
      repoName.className = "nav-repo-name";
      repoName.textContent = REPO_ID;
      brand.insertAdjacentElement("afterend", repoName);
    }
  }

  registerTab("dashboard", "Dashboard");
  registerTab("docs", "Docs");
  registerTab("logs", "Logs");
  registerTab("terminal", "Terminal");

  initTabs();
  initDashboard();
  initGitHub();
  initDocs();
  initLogs();
  initTerminal();

  loadState();
}

async function bootstrap() {
  const { mode } = await detectContext();
  const hubShell = document.getElementById("hub-shell");
  const repoShell = document.getElementById("repo-shell");

  if (mode === "hub") {
    if (hubShell) hubShell.classList.remove("hidden");
    if (repoShell) repoShell.classList.add("hidden");
    initHub();
    return;
  }

  if (repoShell) repoShell.classList.remove("hidden");
  if (hubShell) hubShell.classList.add("hidden");
  initRepoShell();
}

bootstrap();
