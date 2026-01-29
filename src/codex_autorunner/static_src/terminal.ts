import { TerminalManager } from "./terminalManager.js";

let terminalManager: TerminalManager | null = null;

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
  // Ensure terminal is resized to fit container after initialization
  if (terminalManager) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if (typeof (terminalManager as any).fit === "function") {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (terminalManager as any).fit();
    }
  }
}
