import { publish } from "./bus.js";

export function initTabs() {
  const panels = document.querySelectorAll(".panel");
  const tabButtons = document.querySelectorAll(".tab");

  const setActivePanel = (id) => {
    panels.forEach((p) => p.classList.toggle("active", p.id === id));
    tabButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.target === id));
    publish("tab:change", id);
  };

  tabButtons.forEach((btn) =>
    btn.addEventListener("click", () => {
      setActivePanel(btn.dataset.target);
    })
  );
}
