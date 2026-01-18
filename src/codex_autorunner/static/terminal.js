import { TerminalManager } from "./terminalManager.js";
let terminalManager = null;
export function getTerminalManager() {
    return terminalManager;
}
export function initTerminal() {
    if (terminalManager) {
        return;
    }
    terminalManager = new TerminalManager();
    terminalManager.init();
}
