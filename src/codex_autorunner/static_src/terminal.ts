import { TerminalManager } from "./terminalManager.js";

let terminalManager: TerminalManager | null = null;

export function getTerminalManager(): TerminalManager | null {
  return terminalManager;
}

export function initTerminal(): void {
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
