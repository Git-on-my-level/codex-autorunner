// GENERATED FILE - do not edit directly. Source: static_src/
import { TerminalManager } from "./terminalManager.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
import { refreshAgentControls } from "./agentControls.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
import { subscribe } from "./bus.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
import { isRepoHealthy } from "./health.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
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
