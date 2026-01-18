/**
 * Auto-refresh utility for managing periodic data fetching.
 *
 * Features:
 * - Pauses when page is hidden (respects Page Visibility API)
 * - Only refreshes active tab's components
 * - Configurable intervals per component
 * - Immediate refresh on tab activation
 * - Debounces rapid activations
 */

import { subscribe } from "./bus.js";
import { CONSTANTS } from "./constants.js";

interface Refresher {
  callback: () => Promise<void>;
  tabId: string | null;
  interval: number;
  refreshOnActivation: boolean;
  timerId: ReturnType<typeof setInterval> | null;
  lastRefresh: number;
  isRefreshing: boolean;
}

// Track registered refreshers: { id: { callback, interval, tabId, timerId, lastRefresh } }
const refreshers = new Map<string, Refresher>();

// Track current active tab
let activeTab: string | null = null;

// Track page visibility
let pageVisible = true;

/**
 * Register a component for auto-refresh.
 *
 * @param id - Unique identifier for this refresher
 * @param options
 * @param options.callback - Async function to call for refresh
 * @param options.tabId - Tab this refresher belongs to (null for global)
 * @param options.interval - Refresh interval in ms (default: AUTO_REFRESH_INTERVAL)
 * @param options.refreshOnActivation - Refresh when tab becomes active
 * @param options.immediate - Refresh immediately on registration
 * @returns Unregister function
 */
export function registerAutoRefresh(
  id: string,
  options: {
    callback: () => Promise<void>;
    tabId?: string | null;
    interval?: number;
    refreshOnActivation?: boolean;
    immediate?: boolean;
  }
): () => void {
  const {
    callback,
    tabId = null,
    interval = (CONSTANTS.UI?.AUTO_REFRESH_INTERVAL as number | undefined) || 15000,
    refreshOnActivation = true,
    immediate = false,
  } = options;

  const refresher: Refresher = {
    callback,
    tabId,
    interval,
    refreshOnActivation,
    timerId: null,
    lastRefresh: 0,
    isRefreshing: false,
  };

  refreshers.set(id, refresher);

  // Start timer if applicable
  maybeStartTimer(id, refresher);

  // Immediate refresh if requested
  if (immediate) {
    doRefresh(id, refresher);
  }

  return () => unregisterAutoRefresh(id);
}

/**
 * Unregister a refresher.
 */
export function unregisterAutoRefresh(id: string): void {
  const refresher = refreshers.get(id);
  if (refresher) {
    if (refresher.timerId) {
      clearInterval(refresher.timerId);
    }
    refreshers.delete(id);
  }
}

/**
 * Trigger an immediate refresh for a specific refresher.
 */
export function triggerRefresh(id: string): void {
  const refresher = refreshers.get(id);
  if (refresher) {
    doRefresh(id, refresher);
  }
}

/**
 * Check if conditions allow refresh for this refresher.
 */
function canRefresh(refresher: Refresher): boolean {
  // Don't refresh if page is hidden
  if (!pageVisible) return false;

  // Don't refresh if already refreshing
  if (refresher.isRefreshing) return false;

  // If refresher is tab-specific, only refresh when that tab is active
  if (refresher.tabId && refresher.tabId !== activeTab) return false;

  return true;
}

/**
 * Perform actual refresh.
 */
async function doRefresh(id: string, refresher: Refresher): Promise<void> {
  if (!canRefresh(refresher)) return;

  refresher.isRefreshing = true;
  refresher.lastRefresh = Date.now();

  try {
    await refresher.callback();
  } catch (err) {
    console.error(`Auto-refresh error for '${id}':`, err);
  } finally {
    refresher.isRefreshing = false;
  }
}

/**
 * Start or restart interval timer for a refresher.
 */
function maybeStartTimer(id: string, refresher: Refresher): void {
  // Clear existing timer
  if (refresher.timerId) {
    clearInterval(refresher.timerId);
    refresher.timerId = null;
  }

  // Only start timer if page is visible and tab is active (or global)
  if (!pageVisible) return;
  if (refresher.tabId && refresher.tabId !== activeTab) return;

  refresher.timerId = setInterval(() => {
    doRefresh(id, refresher);
  }, refresher.interval);
}

/**
 * Handle tab change - refresh components on new tab and manage timers.
 */
function handleTabChange(tabId: unknown): void {
  activeTab = tabId as string | null;

  refreshers.forEach((refresher, id) => {
    // Stop timers for inactive tabs
    if (refresher.tabId && refresher.tabId !== activeTab) {
      if (refresher.timerId) {
        clearInterval(refresher.timerId);
        refresher.timerId = null;
      }
      return;
    }

    // Start/restart timers for active tab
    maybeStartTimer(id, refresher);

    // Refresh on activation if enabled (with debounce)
    if (refresher.refreshOnActivation) {
      const timeSinceLastRefresh = Date.now() - refresher.lastRefresh;
      // Only refresh if it's been at least 5 seconds since last refresh
      if (timeSinceLastRefresh > 5000) {
        doRefresh(id, refresher);
      }
    }
  });
}

/**
 * Handle page visibility change.
 */
function handleVisibilityChange(): void {
  pageVisible = !document.hidden;

  if (pageVisible) {
    // Page became visible - restart timers and optionally refresh
    refreshers.forEach((refresher, id) => {
      maybeStartTimer(id, refresher);

      // Refresh if it's been a while since last refresh
      const timeSinceLastRefresh = Date.now() - refresher.lastRefresh;
      if (timeSinceLastRefresh > refresher.interval) {
        doRefresh(id, refresher);
      }
    });
  } else {
    // Page hidden - stop all timers
    refreshers.forEach((refresher) => {
      if (refresher.timerId) {
        clearInterval(refresher.timerId);
        refresher.timerId = null;
      }
    });
  }
}

// Initialize event listeners
subscribe("tab:change", handleTabChange);

// Only set up visibility listener if in browser environment
if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("visibilitychange", handleVisibilityChange);
}
