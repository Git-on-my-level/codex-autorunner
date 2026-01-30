import { TerminalManager } from "./terminalManager.js";
import { refreshAgentControls } from "./agentControls.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";

let terminalManager: TerminalManager | null = null;
let terminalHealthRefreshInitialized = false;

export function getTerminalManager(): TerminalManager | null {
  return terminalManager;
}

export function initTerminal(): void {
  if (terminalManager) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if (typeof (terminalManager as any).fit === "function") {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (terminalManager as any).fit();
    }
    return;
  }
  terminalManager = new TerminalManager();
  terminalManager.init();
  initTerminalHealthRefresh();
  // Ensure terminal is resized to fit container after initialization
  if (terminalManager) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if (typeof (terminalManager as any).fit === "function") {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (terminalManager as any).fit();
    }
  }
}

function initTerminalHealthRefresh(): void {
  if (terminalHealthRefreshInitialized) return;
  terminalHealthRefreshInitialized = true;
  subscribe("repo:health", (payload: unknown) => {
    const status = (payload as { status?: string } | null)?.status || "";
    if (status !== "ok" && status !== "degraded") return;
    if (!isRepoHealthy()) return;
    void refreshAgentControls({ reason: "background" });
  });
}
