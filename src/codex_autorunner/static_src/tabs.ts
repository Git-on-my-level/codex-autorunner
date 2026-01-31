import { publish } from "./bus.js";
import { escapeHtml, getUrlParams, updateUrlParams } from "./utils.js";


interface Tab {
  id: string;
  label: string;
  hidden?: boolean;
  menuTab?: boolean;
  icon?: string;
}

interface HamburgerAction {
  id: string;
  label: string;
  icon?: string;
  onClick: () => void;
}

const tabs: Tab[] = [];
const hamburgerActions: HamburgerAction[] = [];
let hamburgerMenuOpen = false;
let hamburgerMenuEl: HTMLElement | null = null;
let hamburgerBtnEl: HTMLButtonElement | null = null;
let hamburgerBackdropEl: HTMLElement | null = null;

export function registerTab(id: string, label: string, opts: { hidden?: boolean; menuTab?: boolean; icon?: string } = {}): void {
  tabs.push({ id, label, hidden: Boolean(opts.hidden), menuTab: Boolean(opts.menuTab), icon: opts.icon });
}

export function registerHamburgerAction(id: string, label: string, icon: string, onClick: () => void): void {
  hamburgerActions.push({ id, label, icon, onClick });
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

function closeHamburgerMenu(): void {
  if (!hamburgerMenuOpen) return;
  hamburgerMenuOpen = false;
  hamburgerMenuEl?.classList.remove("open");
  hamburgerBtnEl?.classList.remove("active");
  hamburgerBackdropEl?.classList.remove("open");
}

function toggleHamburgerMenu(): void {
  hamburgerMenuOpen = !hamburgerMenuOpen;
  hamburgerMenuEl?.classList.toggle("open", hamburgerMenuOpen);
  hamburgerBtnEl?.classList.toggle("active", hamburgerMenuOpen);
  hamburgerBackdropEl?.classList.toggle("open", hamburgerMenuOpen);
}

function updateHamburgerActiveState(activeTabId: string): void {
  if (!hamburgerMenuEl) return;
  const items = hamburgerMenuEl.querySelectorAll(".hamburger-item[data-target]");
  items.forEach((item) => {
    const target = (item as HTMLElement).dataset.target;
    item.classList.toggle("active", target === activeTabId);
  });
  // Also update hamburger button active state if a menu tab is active
  const isMenuTabActive = tabs.some((t) => t.menuTab && t.id === activeTabId);
  hamburgerBtnEl?.classList.toggle("has-active", isMenuTabActive);
}

export function initTabs(defaultTab: string = "analytics"): void {
  const container = document.querySelector(".tabs");
  const navBar = document.querySelector(".nav-bar");
  if (!container) return;

  container.innerHTML = "";

  const panels = document.querySelectorAll(".panel");

  const setActivePanel = (id: string): void => {
    panels.forEach((p) => p.classList.toggle("active", p.id === id));

    // Update primary tab buttons
    const buttons = container.querySelectorAll(".tab");
    buttons.forEach((btn) => btn.classList.toggle("active", (btn as HTMLButtonElement).dataset.target === id));

    // Update hamburger menu items
    updateHamburgerActiveState(id);

    updateUrlParams({ tab: id });
    publish("tab:change", id);
  };

  setActivePanelFn = setActivePanel;

  // Separate primary tabs from menu tabs
  const primaryTabs = tabs.filter((t) => !t.hidden && !t.menuTab);
  const menuTabs = tabs.filter((t) => !t.hidden && t.menuTab);

  // Render primary tabs
  primaryTabs.forEach(tab => {
    const btn = document.createElement("button");
    btn.className = "tab";
    btn.dataset.target = tab.id;
    btn.innerHTML = `
      <span class="tab-label">${escapeHtml(tab.label)}</span>
      <span class="badge hidden" id="tab-badge-${tab.id}"></span>
    `;
    btn.addEventListener("click", () => setActivePanel(tab.id));
    container.appendChild(btn);
  });

  // Create hamburger menu if there are menu tabs or actions
  if (menuTabs.length > 0 || hamburgerActions.length > 0) {
    const wrapper = document.createElement("div");
    wrapper.className = "hamburger-wrapper";

    // Hamburger button
    const btn = document.createElement("button");
    btn.className = "hamburger-btn";
    btn.setAttribute("aria-label", "More options");
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML = `
      <span class="hamburger-icon">
        <span></span>
        <span></span>
        <span></span>
      </span>
    `;
    hamburgerBtnEl = btn;

    // Hamburger menu dropdown
    const menu = document.createElement("div");
    menu.className = "hamburger-menu";
    menu.setAttribute("role", "menu");
    hamburgerMenuEl = menu;

    // Add menu tab items
    menuTabs.forEach((tab) => {
      const item = document.createElement("button");
      item.className = "hamburger-item";
      item.dataset.target = tab.id;
      item.setAttribute("role", "menuitem");
      const iconHtml = tab.icon ? `<span class="hamburger-item-icon">${tab.icon}</span>` : "";
      item.innerHTML = `${iconHtml}<span>${escapeHtml(tab.label)}</span>`;
      item.addEventListener("click", () => {
        setActivePanel(tab.id);
        closeHamburgerMenu();
      });
      menu.appendChild(item);
    });

    // Add divider if there are both tabs and actions
    if (menuTabs.length > 0 && hamburgerActions.length > 0) {
      const divider = document.createElement("div");
      divider.className = "hamburger-divider";
      menu.appendChild(divider);
    }

    // Add action items (like Settings)
    hamburgerActions.forEach((action) => {
      const item = document.createElement("button");
      item.className = "hamburger-item";
      item.dataset.action = action.id;
      item.setAttribute("role", "menuitem");
      const iconHtml = action.icon ? `<span class="hamburger-item-icon">${action.icon}</span>` : "";
      item.innerHTML = `${iconHtml}<span>${escapeHtml(action.label)}</span>`;
      item.addEventListener("click", () => {
        action.onClick();
        closeHamburgerMenu();
      });
      menu.appendChild(item);
    });

    // Mobile backdrop - appended to body for proper z-index stacking
    const backdrop = document.createElement("div");
    backdrop.className = "hamburger-backdrop";
    backdrop.addEventListener("click", closeHamburgerMenu);
    hamburgerBackdropEl = backdrop;
    document.body.appendChild(backdrop);

    // Append menu to body for mobile z-index stacking (above backdrop)
    // On mobile, the nav-bar has z-index:100 which creates a stacking context
    // that would trap the menu below the backdrop (z-index:1999)
    document.body.appendChild(menu);

    // Toggle menu on button click
    const toggleHandler = (e: Event) => {
      e.stopPropagation();
      // Prevent ghost clicks on touch devices
      if (e.type === "touchend") {
        e.preventDefault();
      }
      toggleHamburgerMenu();
      btn.setAttribute("aria-expanded", String(hamburgerMenuOpen));
    };

    btn.addEventListener("click", toggleHandler);
    btn.addEventListener("touchend", toggleHandler);

    // Close menu on outside click (check both wrapper and menu since menu is in body)
    document.addEventListener("click", (e) => {
      if (hamburgerMenuOpen && !wrapper.contains(e.target as Node) && !menu.contains(e.target as Node)) {
        closeHamburgerMenu();
      }
    });

    // Close menu on Escape
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && hamburgerMenuOpen) {
        closeHamburgerMenu();
        hamburgerBtnEl?.focus();
      }
    });

    wrapper.appendChild(btn);

    // Insert hamburger after tabs or at the end of nav bar
    const navActions = navBar?.querySelector(".nav-actions");
    if (navActions) {
      navBar?.insertBefore(wrapper, navActions);
    } else {
      navBar?.appendChild(wrapper);
    }
  }

  const params = getUrlParams();
  const requested = params.get("tab");
  const allVisibleTabs = tabs.filter((t) => !t.hidden);
  const initialTab = allVisibleTabs.some((t) => t.id === requested)
    ? requested
    : allVisibleTabs.some((t) => t.id === defaultTab)
      ? defaultTab
      : allVisibleTabs[0]?.id;
  if (initialTab) {
    setActivePanel(initialTab);
  } else if (allVisibleTabs.length > 0) {
    setActivePanel(allVisibleTabs[0].id);
  }

  if (pendingActivate && allVisibleTabs.some((t) => t.id === pendingActivate)) {
    const id = pendingActivate;
    pendingActivate = null;
    setActivePanel(id);
  }
}
