// GENERATED FILE - do not edit directly. Source: static_src/
import { TerminalManager } from "./terminalManager.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { refreshAgentControls } from "./agentControls.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { subscribe } from "./bus.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { isRepoHealthy } from "./health.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
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
