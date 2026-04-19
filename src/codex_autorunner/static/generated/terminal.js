// GENERATED FILE - do not edit directly. Source: static_src/
import { TerminalManager } from "./terminalManager.js";
import { refreshAgentControls } from "./agentControls.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
let terminalManager = null;
let terminalHealthRefreshInitialized = false;
export function getTerminalManager() {
    return terminalManager;
}
export function initTerminal() {
    if (terminalManager) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        if (typeof terminalManager.fit === "function") {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            terminalManager.fit();
        }
        return;
    }
    terminalManager = new TerminalManager();
    terminalManager.init();
    initTerminalHealthRefresh();
    // Ensure terminal is resized to fit container after initialization
    if (terminalManager) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        if (typeof terminalManager.fit === "function") {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            terminalManager.fit();
        }
    }
}
function initTerminalHealthRefresh() {
    if (terminalHealthRefreshInitialized)
        return;
    terminalHealthRefreshInitialized = true;
    subscribe("repo:health", (payload) => {
        const status = payload?.status || "";
        if (status !== "ok" && status !== "degraded")
            return;
        if (!isRepoHealthy())
            return;
        void refreshAgentControls({ reason: "background" });
    });
}
