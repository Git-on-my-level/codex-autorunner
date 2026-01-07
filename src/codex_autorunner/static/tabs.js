import { publish } from "./bus.js";

const tabs = [];

export function registerTab(id, label) {
  tabs.push({ id, label });
}

export function initTabs(defaultTab = "dashboard") {
  const container = document.querySelector(".tabs");
  if (!container) return;
  
  container.innerHTML = ""; // Clear existing/placeholder tabs

  const panels = document.querySelectorAll(".panel");

  const setActivePanel = (id) => {
    panels.forEach((p) => p.classList.toggle("active", p.id === id));
    
    // Update buttons
    const buttons = container.querySelectorAll(".tab");
    buttons.forEach((btn) => btn.classList.toggle("active", btn.dataset.target === id));
    
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

  if (tabs.some(t => t.id === defaultTab)) {
    setActivePanel(defaultTab);
  } else if (tabs.length > 0) {
    setActivePanel(tabs[0].id);
  }
}
