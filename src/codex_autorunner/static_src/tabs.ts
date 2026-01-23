import { publish } from "./bus.js";
import { getUrlParams, updateUrlParams } from "./utils.js";


interface Tab {
  id: string;
  label: string;
  hidden?: boolean;
}

const tabs: Tab[] = [];

export function registerTab(id: string, label: string, opts: { hidden?: boolean } = {}): void {
  tabs.push({ id, label, hidden: Boolean(opts.hidden) });
}

let setActivePanelFn: ((id: string) => void) | null = null;
let pendingActivate: string | null = null;

export function activateTab(id: string): void {
  if (setActivePanelFn) {
    setActivePanelFn(id);
  } else {
    pendingActivate = id;
  }
}

export function initTabs(defaultTab: string = "dashboard"): void {
  const container = document.querySelector(".tabs");
  if (!container) return;

  container.innerHTML = "";

  const panels = document.querySelectorAll(".panel");

  const setActivePanel = (id: string): void => {
    panels.forEach((p) => p.classList.toggle("active", p.id === id));

    const buttons = container.querySelectorAll(".tab");
    buttons.forEach((btn) => btn.classList.toggle("active", (btn as HTMLButtonElement).dataset.target === id));

    updateUrlParams({ tab: id });
    publish("tab:change", id);
  };

  setActivePanelFn = setActivePanel;

  tabs.forEach(tab => {
    if (tab.hidden) {
      return;
    }
    const btn = document.createElement("button");
    btn.className = "tab";
    btn.dataset.target = tab.id;
    btn.textContent = tab.label;
    btn.addEventListener("click", () => setActivePanel(tab.id));
    container.appendChild(btn);
  });

  const params = getUrlParams();
  const requested = params.get("tab");
  const initialTab = tabs.some((t) => t.id === requested)
    ? requested
    : tabs.some((t) => t.id === defaultTab)
      ? defaultTab
      : tabs[0]?.id;
  if (initialTab) {
    setActivePanel(initialTab);
  } else if (tabs.length > 0) {
    setActivePanel(tabs[0].id);
  }

  if (pendingActivate && tabs.some((t) => t.id === pendingActivate)) {
    const id = pendingActivate;
    pendingActivate = null;
    setActivePanel(id);
  }
}
