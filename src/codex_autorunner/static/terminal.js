import { TerminalManager } from "./terminalManager.js";
let terminalManager = null;
export function getTerminalManager() {
    return terminalManager;
}
export function initTerminal() {
    if (terminalManager) {
        terminalManager.fit();
        return;
    }
    terminalManager = new TerminalManager();
    terminalManager.init();
    // Ensure terminal is resized to fit container after initialization
    if (terminalManager) {
        terminalManager.fit();
    }
}
// export function fitTerminal(): void {
//   if (terminalManager) {
//     terminalManager.fit();
//   }
// }
