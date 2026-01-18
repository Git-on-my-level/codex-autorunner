import { publish } from "./bus.js";
import { getUrlParams, updateUrlParams } from "./utils.js";


interface Tab {
  id: string;
  label: string;
}

const tabs: Tab[] = [];

export function registerTab(id: string, label: string): void {
  tabs.push({ id, label });
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

  tabs.forEach(tab => {
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
}
